# coding=utf-8
from __future__ import absolute_import, division, print_function, unicode_literals

import sys

if sys.version_info[0] >= 3:
    from urllib.request import urlretrieve
else:
    from urllib import urlretrieve

import os
import octoprint.plugin
import phonenumbers
import sarge
from twilio.rest import Client as TwilioRestClient
from twilio.base import values


__plugin_pythoncompat__ = ">=2.7,<4"


class SMSNotifierPlugin(octoprint.plugin.EventHandlerPlugin,
                        octoprint.plugin.SettingsPlugin,
                        octoprint.plugin.TemplatePlugin):

    # SettingsPlugin

    def get_settings_defaults(self):
        # matching password must be registered in system keyring
        # to support customizable mail server, may need port too
        return dict(
            enabled=False,
            send_image=False,
            recipient_number="",
            from_number="",
            account_sid="",
            auth_token="",
            printer_name="",
            on_print_pause=False,
            on_print_done=True,
            message_format=dict(
                body="{printer_name} event: {event} - {filename} - {elapsed_time}"
            )
        )

    def get_settings_version(self):
        return 1

    # TemplatePlugin

    def get_template_configs(self):
        return [
            dict(type="settings", name="SMS Notifier", custom_bindings=False)
        ]

    # EventPlugin

    def on_event(self, event, payload):
        if event != "PrintDone" or event != "PrintPause" :
            return

        if not self._settings.get(['enabled']):
            return

        if event == "PrintDone" and not self._settings.get(['on_print_done']):
            return

        if event == "PrintPause" and not self._settings.get(['on_print_pause']):
            return

        payload["event"] = event

        if self._settings.get(['send_image']):
            snapshot_url = self._settings.global_get(["webcam", "snapshot"])
            if snapshot_url:
                self._logger.info("Taking Snapshot.... Say Cheese!")
                try:
                    snapshot_path, headers = urlretrieve(snapshot_url)
                except Exception as e:
                    self._logger.exception("Exception while fetching snapshot from webcam, sending only a note: {message}".format(
                        message=str(e)))
                else:
                    # ffmpeg can't guess file type it seems
                    os.rename(snapshot_path, snapshot_path + ".jpg")
                    snapshot_path += ".jpg"
                    # flip or rotate as needed
                    self._logger.info("Processing %s before uploading." % snapshot_path)
                    self._process_snapshot(snapshot_path)

                    try:
                        from cloudinary import uploader
                        response = uploader.unsigned_upload(snapshot_path, "snapshot", cloud_name="octoprint-twilio")
                    except Exception as e:
                        self._logger.exception("Error Uploading image to the cloud: {message}".format(message=str(e)))
                        return self._sent_text(payload)
                    else:
                        if "url" in response:
                            self._logger.info("Snapshot uploaded to {}".format(response["url"]))
                            if self._send_txt(payload, response['url']):
                                return True
                            else:
                                self._logger.warn("Could not send a webcam image, sending only text notification.")
                                return self._send_txt(payload)
                        else:
                            self._logger.error("Cloud returned {}".format(response["error"]["message"]))
                            return self._send_txt(payload)
            self._logger.warn("Could not find settings for snapshot URL. Is it enabled?")
            return self._send_txt(payload)

        else:
            return self._send_txt(payload)

    def _send_txt(self, payload, media_url=values.unset):

        filename = payload["name"]
        event = payload["event"]

        import datetime
        import octoprint.util
        elapsed_time = octoprint.util.get_formatted_timedelta(datetime.timedelta(seconds=payload["time"]))

        fromnumber = phonenumbers.format_number(phonenumbers.parse(self._settings.get(['from_number']), 'US'), phonenumbers.PhoneNumberFormat.E164)

        tags = {
            'event': event,
            'filename': filename,
            'elapsed_time': elapsed_time,
            'printer_name': self._settings.get(["printer_name"])
        }
        message = self._settings.get(["message_format", "body"]).format(**tags)

        client = TwilioRestClient(self._settings.get(['account_sid']), self._settings.get(['auth_token']))

        for number in self._settings.get(['recipient_number']).split(','):
            tonumber = phonenumbers.format_number(phonenumbers.parse(number, 'US'), phonenumbers.PhoneNumberFormat.E164)

            try:
                client.messages.create(to=tonumber, from_=fromnumber, body=message, media_url=media_url)
            except Exception as e:
                # report problem sending sms
                self._logger.error("SMS notification error: %s" % (str(e)))
                continue
            else:
                # report notification was sent
                self._logger.info("Print notification sent to %s" % (tonumber))

        # all messages were attempted to be sent
        return True

    def _process_snapshot(self, snapshot_path, pixfmt="yuv420p"):
        hflip = self._settings.global_get_boolean(["webcam", "flipH"])
        vflip = self._settings.global_get_boolean(["webcam", "flipV"])
        rotate = self._settings.global_get_boolean(["webcam", "rotate90"])
        ffmpeg = self._settings.global_get(["webcam", "ffmpeg"])

        if not ffmpeg or not os.access(ffmpeg, os.X_OK) or (not vflip and not hflip and not rotate):
            return

        ffmpeg_command = [ffmpeg, "-y", "-i", snapshot_path]

        rotate_params = ["format={}".format(pixfmt)]  # workaround for foosel/OctoPrint#1317
        if rotate:
            rotate_params.append("transpose=2")  # 90 degrees counter clockwise
        if hflip:
            rotate_params.append("hflip")       # horizontal flip
        if vflip:
            rotate_params.append("vflip")       # vertical flip

        ffmpeg_command += ["-vf", sarge.shell_quote(",".join(rotate_params)), snapshot_path]
        self._logger.info("Running: {}".format(" ".join(ffmpeg_command)))

        p = sarge.run(ffmpeg_command, stdout=sarge.Capture(), stderr=sarge.Capture())
        if p.returncode == 0:
            self._logger.info("Rotated/flipped image with ffmpeg")
        else:
            self._logger.warn("Failed to rotate/flip image with ffmpeg, "
                              "got return code {}: {}, {}".format(p.returncode,
                                                                  p.stdout.text,
                                                                  p.stderr.text))

    def get_update_information(self):
        return dict(
            smsnotifier=dict(
                displayName="SMSNotifier Plugin",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="taxilian",
                repo="OctoPrint-Twilio",
                current=self._plugin_version,

                # update method: pip
                pip="https://github.com/taxilian/OctoPrint-Twilio/archive/{target_version}.zip",
                dependency_links=False
            )
        )


__plugin_name__ = "SMS Notifier (with Twilio)"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = SMSNotifierPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
