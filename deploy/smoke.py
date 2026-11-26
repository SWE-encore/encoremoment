"""Send an approved WAV (or the loudness fixture) and check FINAL replay and idempotency."""
import argparse
from array import array
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4
import wave

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(CONTRACTS / "reference"))
from encore_contract import encode_audio_frame


def audio_blocks(wav):
    if wav:
        with wave.open(str(wav), "rb") as stream:
            if (stream.getnchannels(), stream.getframerate(), stream.getsampwidth()) != (1, 16000, 2):
                raise ValueError("Use mono 16 kHz PCM16 WAV")
            if not 15600 <= stream.getnframes() <= 60 * 16000:
                raise ValueError("Use a recording between 0.975 and 60 seconds")
            while pcm := stream.readframes(8000):
                yield pcm
    else:
        tone = array("h", (round(16000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(8000)))
        if sys.byteorder != "little":
            tone.byteswap()
        yield from [tone.tobytes()] * 4


def replay(ws_url, control, after=0):
    from websockets.sync.client import connect

    # The server sends every replayed event before processing the repeated control.
    events = []
    with connect(f"{ws_url}?after={after}", open_timeout=10) as ws:
        ws.send(control)
        while True:
            msg = json.loads(ws.recv(timeout=30))
            if msg["kind"] == "control_ack":
                if msg["status"] != "duplicate":
                    raise RuntimeError(f"Retry acknowledgement: {msg}")
                return events
            if msg["kind"] == "event_update":
                events.append(msg)


def pc_fixture(server):
    """Run the desktop entry point and verify the event the PC actually displayed."""
    with tempfile.TemporaryDirectory() as directory:
        wav = Path(directory) / "tone.wav"
        log = Path(directory) / "session.jsonl"
        with wave.open(str(wav), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            for pcm in audio_blocks(None):
                stream.writeframes(pcm)
        result = subprocess.run(
            [sys.executable, "-m", "encore_client", "--headless", "--no-player",
             "--mic-wav", str(wav), "--server", server, "--log", str(log), "--timeout", "3"],
            cwd=CONTRACTS.parent / "client", timeout=25, stdout=subprocess.PIPE, text=True,
        )
        print(result.stdout, file=sys.stderr, end="")
        result.check_returncode()
        records = [json.loads(line) for line in log.read_text().splitlines()]

    incoming = [row["msg"] for row in records if row["what"] == "in"]
    finals = [msg for msg in incoming if msg["kind"] == "event_update" and msg["event"]["phase"] == "FINAL"]
    if len(finals) != 1:
        raise RuntimeError(f"Expected one FINAL at the PC; received {len(finals)}")
    final = finals[0]
    event = final["event"]
    displayed = [line.split() for line in result.stdout.splitlines()]
    expected_row = [event["event_id"], "laughter", "FINAL", "rev", str(event["revision"])]
    if (expected_row not in displayed or event["source"]["model"] != "loudness-standin"
            or "audio sent 4 dropped 0" not in result.stdout):
        raise RuntimeError("The PC did not display the fixture FINAL with four delivered chunks")
    anchor = next(row["msg"] for row in records if row["what"] == "out" and row["msg"]["kind"] == "playback_anchor")
    playback = event["playback"]
    if (playback.get("content_id") != "mic-clock" or
            any(playback.get(f"timecode_{edge}_ms") != event["capture"][f"{edge}_ms"] - anchor["capture_ms"]
                for edge in ("start", "end"))):
        raise RuntimeError(f"PC event timecode does not match the microphone clock: {playback}")
    print(json.dumps({"session_id": final["session_id"], "final_event_ids": [event["event_id"]],
                      "timecode_start_ms": playback["timecode_start_ms"], "timecode_end_ms": playback["timecode_end_ms"]}))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", default="ws://127.0.0.1:8765")
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--pc", action="store_true", help="use the actual desktop client with the loudness fixture")
    source.add_argument("--wav", type=Path, help="approved reaction recording for the YAMNet image")
    ap.add_argument("--expect", choices=("laughter", "scream", "gasp"), default="laughter")
    args = ap.parse_args()
    if args.pc:
        if args.expect != "laughter":
            ap.error("--pc expects laughter from the fixture")
        pc_fixture(args.server)
        return

    from websockets.sync.client import connect

    sid = f"smoke-{uuid4().hex}"
    ws_url = f"{args.server.rstrip('/')}/v1/sessions/{sid}/stream"
    sample = json.loads((CONTRACTS / "samples/07_microphone_audio.json").read_text())
    control = sample["messages"][0]["msg"] | {"session_id": sid, "capture_ms": 0}
    header = sample["messages"][2]["msg"] | {"session_id": sid}
    with connect(ws_url, open_timeout=10) as ws:
        for expected in ("applied", "duplicate"):
            ws.send(json.dumps(control))
            ack = json.loads(ws.recv(timeout=10))
            if ack["status"] != expected:
                raise RuntimeError(f"Microphone control: {ack}")
        n_samples = 0
        next_seq = 1
        for i, pcm in enumerate(audio_blocks(args.wav)):
            frame = encode_audio_frame(header | {
                "seq": i + 1, "chunk": i, "samples": len(pcm) // 2, "capture_ms": n_samples // 16,
            }, pcm)
            n_samples += len(pcm) // 2
            next_seq = i + 2
            ws.send(frame)
            ws.send(frame)
        off = json.dumps(control | {
            "seq": next_seq, "generation": 2, "enabled": False,
            "idempotency_key": "ctl-microphone-2", "capture_ms": n_samples // 16,
        })
        ws.send(off)
        while True:
            reply = json.loads(ws.recv(timeout=30))
            if reply["kind"] == "control_ack":
                if reply["status"] != "applied":
                    raise RuntimeError(f"Microphone stop: {reply}")
                break

    results = replay(ws_url, off)
    finals = [msg["event"] for msg in results if msg["event"]["phase"] == "FINAL"]
    model = "google/yamnet/1" if args.wav else "loudness-standin"
    # A loaded model's name ends in its weights' digest: google/yamnet/1@sha256:...
    matches = [ev for ev in finals if ev["reaction"]["type"] == args.expect
               and ev["source"]["model"].partition("@")[0] == model]
    if not matches or (not args.wav and len(finals) != 1):
        raise RuntimeError(f"Expected {args.expect} from {model}; got {finals}")
    if [m["cursor"] for m in results] != list(range(1, len(results) + 1)):
        raise RuntimeError("Replayed cursor sequence is incomplete")
    if replay(ws_url, off, results[-1]["cursor"]):
        raise RuntimeError("Replay after the last cursor produced extra events")
    print(json.dumps({"session_id": sid, "model": model, "last_cursor": results[-1]["cursor"],
                      "final_event_ids": [ev["event_id"] for ev in finals]}))


if __name__ == "__main__":
    main()
