"""End-to-end test rig: a real OctoPrint + its virtual printer + this plugin.

Run inside a virtualenv that has OctoPrint installed plus this plugin
(``pip install -e . --no-build-isolation``). OctoPrint is started from its own
basedir, never from this checkout, so the plugin under test is always the one
installed in the virtualenv (``python -m`` puts the cwd on sys.path).
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile

import pytest
import requests
import yaml

API_KEY = "backupschedulertestkey0123456789ab"
HERE = os.path.dirname(os.path.abspath(__file__))

# Bundled plugins that only add startup time or talk to the internet.
DISABLED_PLUGINS = [
	"announcements",
	"errortracking",
	"health_check",
	"pluginmanager",
	"softwareupdate",
	"tracking",
	"discovery",
]


def _free_port():
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


class OctoPrintEnv:
	def __init__(self, tmp_path, plugin_settings, hold_print_done=True):
		self.basedir = tmp_path / "octoprint"
		self.basedir.mkdir()
		self.port = _free_port()
		self.base = "http://127.0.0.1:%d" % self.port
		self.proc = None
		self.stdout = None
		self._write_config(plugin_settings, hold_print_done)

	def _write_config(self, plugin_settings, hold_print_done):
		cfg = {
			"api": {"key": API_KEY},
			"server": {
				"host": "127.0.0.1",
				"port": self.port,
				"firstRun": False,
				"onlineCheck": {"enabled": False},
				"pluginBlacklist": {"enabled": False},
			},
			"feature": {"sdSupport": False},
			"webcam": {"webcamEnabled": False, "timelapseEnabled": False},
			"serial": {"port": "VIRTUAL", "baudrate": 115200, "autoconnect": False},
			"plugins": {
				"_disabled": DISABLED_PLUGINS,
				"virtual_printer": {"enabled": True},
				"backupscheduler": plugin_settings,
				"backup_trigger": {"hold_print_done": hold_print_done},
			},
		}
		with open(self.basedir / "config.yaml", "w") as f:
			yaml.safe_dump(cfg, f)

	def start(self):
		plugins_dir = self.basedir / "plugins"
		plugins_dir.mkdir(exist_ok=True)
		shutil.copy(os.path.join(HERE, "support", "backup_trigger.py"), plugins_dir / "backup_trigger.py")

		self.stdout = open(self.basedir / "stdout.log", "w")
		self.proc = subprocess.Popen(
			[sys.executable, "-m", "octoprint", "--basedir", str(self.basedir), "serve",
			 "--host", "127.0.0.1", "--port", str(self.port)],
			stdout=self.stdout, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONUNBUFFERED="1"),
			cwd=str(self.basedir),
		)
		if not self.wait_for(self._up, timeout=90):
			raise AssertionError("OctoPrint never came up:\n" + self.diagnostics())

	def stop(self):
		if self.proc and self.proc.poll() is None:
			self.proc.terminate()
			try:
				self.proc.wait(20)
			except subprocess.TimeoutExpired:
				self.proc.kill()
		if self.stdout:
			self.stdout.close()

	def api(self, method, path, **kw):
		headers = kw.pop("headers", {})
		headers["X-Api-Key"] = API_KEY
		return requests.request(method, self.base + path, headers=headers, timeout=30, **kw)

	def _up(self):
		try:
			return self.api("GET", "/api/plugin/backup_trigger").status_code == 200
		except requests.RequestException:
			return False

	def wait_for(self, predicate, timeout=45, interval=0.4):
		end = time.time() + timeout
		while time.time() < end:
			try:
				if predicate():
					return True
			except requests.RequestException:
				pass
			time.sleep(interval)
		return False

	def printer_state(self):
		return self.api("GET", "/api/connection").json()["current"]["state"]

	def events_seen(self):
		return self.api("GET", "/api/plugin/backup_trigger").json()["events"]

	def scheduler_status(self):
		return self.api("GET", "/api/plugin/backup_trigger").json()

	def trigger(self, command, **data):
		r = self.api("POST", "/api/plugin/backup_trigger", json=dict(command=command, **data))
		assert r.status_code == 200, r.text
		return r.json()

	def plugin_setting(self, key):
		return self.api("GET", "/api/settings").json()["plugins"]["backupscheduler"][key]

	def backup_file(self, name):
		return self.basedir / "data" / "backup" / name

	def log_text(self):
		try:
			return (self.basedir / "logs" / "octoprint.log").read_text(errors="replace")
		except FileNotFoundError:
			return ""

	def diagnostics(self):
		tail = "\n".join(self.log_text().splitlines()[-60:])
		try:
			out = (self.basedir / "stdout.log").read_text(errors="replace")[-1500:]
		except FileNotFoundError:
			out = ""
		return "---- octoprint.log (tail) ----\n%s\n---- stdout ----\n%s" % (tail, out)


@pytest.fixture
def make_env(tmp_path):
	envs = []

	def factory(hold_print_done=True, seed_backups=(), **plugin_settings):
		"""seed_backups: names of existing backup zips to create in OctoPrint's backup folder."""
		env = OctoPrintEnv(tmp_path, plugin_settings, hold_print_done=hold_print_done)
		for name in seed_backups:
			path = env.backup_file(name)
			path.parent.mkdir(parents=True, exist_ok=True)
			with zipfile.ZipFile(path, "w") as z:
				z.writestr("seed.txt", "an earlier, good backup")
		envs.append(env)
		env.start()
		return env

	yield factory
	for env in envs:
		env.stop()
