# coding=utf-8
"""Test-support plugin: drives Backup Scheduler and OctoPrint's backup plugin from a test.

POST /api/plugin/backup_trigger with:

- ``{"command": "backup", "backup_type": "daily_backups"}`` calls Backup Scheduler's
  ``_perform_backup`` exactly as its schedule does. While a print is running that defers the
  backup until the print ends, the same as a scheduled backup that comes due mid-print. With
  ``"background": true`` it runs on its own thread (as the schedule's timer does) and the request
  returns at once.
- ``{"command": "fail", "mode": "space"|"write"|""}`` makes OctoPrint's backups fail: "space"
  reports too little free space (raised before the after_backup hook runs), "write" makes
  writing the zip fail (the after_backup hook gets error=True), "" stops failing.
- ``{"command": "slow", "seconds": N}`` makes every backup take at least N seconds longer.

GET /api/plugin/backup_trigger lists the events this plugin has received, so a test can tell
whether OctoPrint is still delivering events, and Backup Scheduler's pending state.

PrintDone is fired while the printer is still "Finishing", and events are delivered
asynchronously, so whether the next handler sees a finished printer is a race. On a real
instance other plugins' handlers usually take long enough that it doesn't. To make that
deterministic, this plugin holds PrintDone until the printer has left "Finishing" (setting
``hold_print_done``, default on). OctoPrint calls on_event in plugin-identifier order, and
"backup_trigger" sorts before "backupscheduler". With the hold off, Backup Scheduler usually
sees PrintDone while the printer is still "Finishing".
"""

import threading
import time
import types
import zipfile

import flask
import octoprint.plugin
import octoprint.plugins.backup as core_backup

__plugin_name__ = "Backup trigger (test support)"
__plugin_pythoncompat__ = ">=3.7,<4"

_REAL_FREE_SPACE = core_backup.BackupPlugin.__dict__["_free_space"]


def _failing_zipfile(*args, **kwargs):
	raise OSError("test: writing the backup zip fails")


class BackupTriggerPlugin(octoprint.plugin.SimpleApiPlugin, octoprint.plugin.EventHandlerPlugin,
						  octoprint.plugin.SettingsPlugin):
	def __init__(self):
		self._events = []
		self._lock = threading.Lock()
		self._slow = 0

	def get_settings_defaults(self):
		return {"hold_print_done": True}

	def is_api_protected(self):
		return True

	def get_api_commands(self):
		return {"backup": ["backup_type"], "fail": ["mode"], "slow": ["seconds"]}

	def _scheduler(self):
		return self._plugin_manager.get_plugin_info("backupscheduler").implementation

	def on_api_command(self, command, data):
		scheduler = self._scheduler()
		if command == "backup":
			if data.get("background"):
				t = threading.Thread(target=scheduler._perform_backup, kwargs={"backup_type": data["backup_type"]})
				t.daemon = True
				t.start()
			else:
				scheduler._perform_backup(backup_type=data["backup_type"])
		elif command == "fail":
			mode = data["mode"]
			core_backup.BackupPlugin._free_space = (
				classmethod(lambda cls, path, size: False) if mode == "space" else _REAL_FREE_SPACE)
			if mode == "write":
				core_backup.zipfile = types.SimpleNamespace(
					ZipFile=_failing_zipfile, ZIP_DEFLATED=zipfile.ZIP_DEFLATED, ZIP_STORED=zipfile.ZIP_STORED)
			else:
				core_backup.zipfile = zipfile
		elif command == "slow":
			self._slow = float(data["seconds"])
		return flask.jsonify(self._status())

	def _status(self):
		scheduler = self._scheduler()
		return {"pending": list(scheduler.backup_pending_type), "backup_pending": scheduler.backup_pending}

	def on_api_get(self, request):
		with self._lock:
			status = self._status()
			status["events"] = list(self._events)
			return flask.jsonify(status)

	def on_event(self, event, payload):
		if event == "PrintDone" and self._settings.get_boolean(["hold_print_done"]):
			end = time.monotonic() + 10
			while self._printer.is_finishing() and time.monotonic() < end:
				time.sleep(0.05)
		with self._lock:
			self._events.append(event)

	def before_backup(self, *args, **kwargs):
		if self._slow:
			time.sleep(self._slow)


__plugin_implementation__ = BackupTriggerPlugin()
__plugin_hooks__ = {
	"octoprint.plugin.backup.before_backup": __plugin_implementation__.before_backup,
}
