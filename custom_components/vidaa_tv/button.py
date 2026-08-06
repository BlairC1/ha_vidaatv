#!/usr/bin/env python3
"""
vidaa_key_probe.py - hunt for an undocumented remote key (e.g. Audio Only).

There is no published list of VIDAA key names: every list in the wild
(Krazy998, hisensetv, pyvidaa) was assembled by trying names and keeping the
ones that worked. The TV silently ignores names it does not recognise, so
sweeping candidates is safe.

Sends each candidate with a pause between them and prints any state broadcast
that follows, so a key that changes something is visible in the output as well
as on the screen.

WATCH THE TV while this runs - the panel blanking is the signal you want, and
audio-only may not produce any broadcast at all.

  python3 vidaa_key_probe.py --ip <IP> --cert <pem> --key <key> --mac <MAC>
  python3 vidaa_key_probe.py ... --delay 3          # slower sweep
  python3 vidaa_key_probe.py ... --keys KEY_A,KEY_B # test specific names
"""
import argparse
import json
import time

from pyvidaa.client import VidaaTV
from pyvidaa.protocol import AuthMethod

# Ordered by how likely they seem for a picture-off / audio-only button.
CANDIDATES = [
    # direct names
    "KEY_AUDIOONLY", "KEY_AUDIO_ONLY", "KEY_AUDIOMODE", "KEY_AUDIO_MODE",
    "KEY_ONLYAUDIO", "KEY_ONLY_AUDIO", "KEY_AUDIO",
    # picture / screen off
    "KEY_PICTUREOFF", "KEY_PICTURE_OFF", "KEY_PICTUREMODE", "KEY_PICTURE",
    "KEY_SCREENOFF", "KEY_SCREEN_OFF", "KEY_SCREEN", "KEY_DISPLAYOFF",
    "KEY_DISPLAY_OFF", "KEY_DISPLAY", "KEY_PANELOFF", "KEY_PANEL_OFF",
    "KEY_BLANK", "KEY_BLANKSCREEN", "KEY_BLACKSCREEN", "KEY_VIDEOOFF",
    "KEY_VIDEO_OFF", "KEY_NOPICTURE",
    # power saving / eco, which is where some sets put it
    "KEY_ECO", "KEY_ECOMODE", "KEY_ENERGY", "KEY_ENERGYSAVING",
    "KEY_POWERSAVE", "KEY_POWERSAVING", "KEY_BACKLIGHT", "KEY_BACKLIGHTOFF",
    # linux input-event style names VIDAA may reuse
    "KEY_SWITCHVIDEOMODE", "KEY_VIDEO", "KEY_MEDIA", "KEY_SOUND",
    "KEY_MODE", "KEY_TOGGLE_SCREEN",
    # misc buttons that sometimes carry it
    "KEY_GREEN", "KEY_YELLOW", "KEY_BLUE", "KEY_RED",
    "KEY_SETTINGS", "KEY_QUICKMENU", "KEY_TOOLS", "KEY_OPTION",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--mac", required=True)
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between keys (raise to watch the screen)")
    ap.add_argument("--keys", help="comma-separated list to test instead")
    args = ap.parse_args()

    keys = args.keys.split(",") if args.keys else CANDIDATES

    tv = VidaaTV(
        host=args.ip, certfile=args.cert, keyfile=args.key,
        mac_address=args.mac, use_dynamic_auth=True,
        auth_method=AuthMethod.MODERN, enable_persistence=False,
        verify_ssl=False,
    )
    if not tv.connect(timeout=12):
        raise SystemExit("connect FAILED - check --mac and the cert paths")
    print(f"CONNECTED as {tv.client_id}\n")

    seen: list[tuple[float, str]] = []
    paho = tv._client
    previous = paho.on_message

    def hook(client, userdata, msg):
        if "ui_service/state" in msg.topic:
            seen.append(
                (time.monotonic(), msg.payload.decode("utf-8", "replace")[:160])
            )
        if previous:
            previous(client, userdata, msg)

    paho.on_message = hook
    paho.subscribe("#", qos=0)
    time.sleep(2)

    print(f"Sweeping {len(keys)} candidates, {args.delay}s apart.")
    print("WATCH THE TV - a blanked panel is the result you are looking for.\n")

    for name in keys:
        name = name.strip()
        if not name:
            continue
        # Print BEFORE sending. Printing afterwards meant the last line on
        # screen named the PREVIOUS key while the current one was still in its
        # delay - so interrupting the moment the TV reacted pointed at the wrong
        # key entirely.
        print(f"  sending {name:24}", end="", flush=True)
        mark = time.monotonic()
        seen.clear()
        try:
            tv.send_key(name)
        except Exception as err:  # noqa: BLE001
            print(f" send failed: {err}")
            continue
        time.sleep(args.delay)
        reactions = [p for t, p in seen if t > mark]
        if reactions:
            print(" *** TV REACTED ***")
            for payload in reactions[:2]:
                print(f"      {payload}")
        else:
            print(" -")

    tv.disconnect()
    print(
        "\nDone. A key that blanked the screen is the one you want - note that\n"
        "it may show '-' above, since the TV does not broadcast every change."
    )


if __name__ == "__main__":
    main()
