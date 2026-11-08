# coding=utf-8
"""Test-support plugin: lets a test ask Backup Scheduler for a backup and see which events arrive.

POST /api/plugin/backup_trigger {"command": "backup", "backup_type": "daily_backups"} calls
Backup Scheduler's ``_perform_backup`` exactly as its schedule does. While a print is running
that defers the backup until the print ends, the same as a scheduled backup that comes due
mid-print. GET /api/plugin/backup_trigger lists the events this plugin has received, so a test
can tell whether OctoPrint is still delivering events.

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

import flask
import octoprint.plugin

__plugin_name__ = "Backup trigger (test support)"
__plugin_pythoncompat__ = ">=3.7,<4"


class BackupTriggerPlugin(octoprint.plugin.SimpleApiPlugin, octoprint.plugin.EventHandlerPlugin,
						  octoprint.plugin.SettingsPlugin):
	def __init__(self):
		self._events = []
		self._lock = threading.Lock()

	def get_settings_defaults(self):
		return {"hold_print_done": True}

	def is_api_protected(self):
		return True

	def get_api_commands(self):
		return {"backup": ["backup_type"]}

	def on_api_command(self, command, data):
		info = self._plugin_manager.get_plugin_info("backupscheduler")
		info.implementation._perform_backup(backup_type=data["backup_type"])
		return flask.jsonify({"pending": info.implementation.backup_pending_type})

	def on_api_get(self, request):
		with self._lock:
			return flask.jsonify({"events": list(self._events)})

	def on_event(self, event, payload):
		if event == "PrintDone" and self._settings.get_boolean(["hold_print_done"]):
			end = time.monotonic() + 10
			while self._printer.is_finishing() and time.monotonic() < end:
				time.sleep(0.05)
		with self._lock:
			self._events.append(event)


__plugin_implementation__ = BackupTriggerPlugin()
