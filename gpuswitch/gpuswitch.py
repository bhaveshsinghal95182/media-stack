"""Pick Jellyfin's transcoding hardware by power source.

On AC power Jellyfin uses AC_HWACCEL (nvenc: the NVIDIA GPU); on battery it
uses BATTERY_HWACCEL (qsv: the Intel iGPU, far cheaper on power). Checks every
INTERVAL seconds and only writes Jellyfin's encoding config when it changes.
When AC is lost, transcodes still running on the AC hardware are stopped so
the NVIDIA GPU goes back to sleep; the client restarts playback on QSV.

Stdlib only. Reads power state from sysfs, which containers see read-only.
"""

import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
API_KEY = os.environ["JELLYFIN_API_KEY"]
AC_HWACCEL = os.environ.get("AC_HWACCEL", "nvenc")
BATTERY_HWACCEL = os.environ.get("BATTERY_HWACCEL", "qsv")
INTERVAL = int(os.environ.get("INTERVAL", "15"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Both the Intel UHD (Raptor Lake) and the RTX 4050 decode all of these in hardware.
DECODE_CODECS = ["h264", "hevc", "mpeg2video", "vc1", "vp8", "vp9", "av1"]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def on_ac():
    """True on mains power. None if no mains supply is visible (unknown)."""
    seen = False
    for d in glob.glob("/sys/class/power_supply/*"):
        try:
            with open(f"{d}/type") as f:
                if f.read().strip() != "Mains":
                    continue
            with open(f"{d}/online") as f:
                seen = True
                if f.read().strip() == "1":
                    return True
        except OSError:
            continue
    return False if seen else None


def intel_render_node():
    """The Intel render node; numbering can change between boots."""
    for d in sorted(glob.glob("/sys/class/drm/renderD*")):
        try:
            with open(f"{d}/device/vendor") as f:
                if f.read().strip() == "0x8086":
                    return "/dev/dri/" + os.path.basename(d)
        except OSError:
            continue
    return None


def api(method, path, body=None):
    req = urllib.request.Request(
        JELLYFIN_URL + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f'MediaBrowser Token="{API_KEY}"',
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    return json.loads(data) if data else None


def desired(mode):
    want = {
        "HardwareAccelerationType": mode,
        "EnableHardwareEncoding": True,
        "HardwareDecodingCodecs": DECODE_CODECS if mode != "none" else [],
        "EnableDecodingColorDepth10Hevc": True,
        "EnableDecodingColorDepth10Vp9": True,
        "EnableTonemapping": mode != "none",
        "EnableVppTonemapping": mode == "qsv",
        # Encode only a few minutes ahead of the viewer instead of racing
        # through the whole film, and drop segments already watched.
        "EnableThrottling": True,
        "EnableSegmentDeletion": True,
    }
    if mode == "qsv":
        node = intel_render_node()
        if node is None:
            raise RuntimeError("no Intel render node visible")
        want["QsvDevice"] = node
    return want


def stop_transcodes(mode):
    """Stop active transcodes that use `mode` hardware."""
    for s in api("GET", "/Sessions") or []:
        t = s.get("TranscodingInfo") or {}
        if t.get("HardwareAccelerationType", "").lower() != mode or not s.get("DeviceId"):
            continue
        q = {"deviceId": s["DeviceId"]}
        if (s.get("PlayState") or {}).get("PlaySessionId"):
            q["playSessionId"] = s["PlayState"]["PlaySessionId"]
        name = (s.get("NowPlayingItem") or {}).get("Name", "?")
        log(f"stopping {mode} transcode of {name!r} for {s.get('UserName')} on {s.get('Client')}")
        if not DRY_RUN:
            api("DELETE", "/Videos/ActiveEncodings?" + urllib.parse.urlencode(q))


def tick(state):
    ac = on_ac()
    mode = AC_HWACCEL if ac is not False else BATTERY_HWACCEL
    cfg = api("GET", "/System/Configuration/encoding")
    want = desired(mode)
    diff = {k: v for k, v in want.items() if cfg.get(k) != v}
    if diff:
        power = {True: "AC", False: "battery", None: "unknown power (treated as AC)"}[ac]
        log(f"{power}: setting {', '.join(f'{k}={v}' for k, v in diff.items())}")
        if not DRY_RUN:
            api("POST", "/System/Configuration/encoding", {**cfg, **want})
    if state.get("mode") == AC_HWACCEL and mode != AC_HWACCEL and AC_HWACCEL != BATTERY_HWACCEL:
        stop_transcodes(AC_HWACCEL)
    state["mode"] = mode


def main():
    log(f"gpuswitch: AC → {AC_HWACCEL}, battery → {BATTERY_HWACCEL}, every {INTERVAL}s{' (dry run)' if DRY_RUN else ''}")
    state, last_err = {}, None
    while True:
        try:
            tick(state)
            last_err = None
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as e:
            # Jellyfin restarting, etc. Log each distinct error once.
            if str(e) != last_err:
                log(f"error: {e}")
                last_err = str(e)
        if "--once" in sys.argv:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
