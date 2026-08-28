from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
SCENES_PATH = ROOT / "scenes.json"
RATE = 24_000
GAP_SECONDS = 0.45


def timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def caption_chunks(text: str) -> list[str]:
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？])", text)
        if part.strip()
    ]
    chunks: list[str] = []
    for sentence in sentences:
        if len(sentence) <= 24:
            chunks.append(sentence)
            continue
        pieces = [part for part in re.split(r"(?<=[，、：；])", sentence) if part]
        current = ""
        for piece in pieces:
            if current and len(current + piece) > 24:
                chunks.append(current)
                current = piece
            else:
                current += piece
        if current:
            chunks.append(current)
    return chunks


def main() -> None:
    scenes = json.loads(SCENES_PATH.read_text(encoding="utf-8"))
    kokoro = Kokoro(
        str(ASSETS / "kokoro-v1.0.int8.onnx"),
        str(ASSETS / "voices-v1.0.bin"),
    )
    silence = np.zeros(round(RATE * GAP_SECONDS), dtype=np.float32)
    audio_parts: list[np.ndarray] = []
    current = 0.0
    cues: list[tuple[float, float, str]] = []
    script_lines: list[str] = []

    for scene in scenes:
        samples, sample_rate = kokoro.create(
            scene["narration"],
            voice="zf_xiaobei",
            speed=0.97,
            lang="cmn",
        )
        if sample_rate != RATE:
            raise RuntimeError(f"unexpected sample rate: {sample_rate}")
        samples = np.asarray(samples, dtype=np.float32)
        sf.write(ASSETS / f"scene-{scene['id']}.wav", samples, RATE)
        scene["start"] = round(current, 3)
        spoken_duration = len(samples) / RATE
        scene["spokenDuration"] = round(spoken_duration, 3)
        scene["duration"] = round(spoken_duration + GAP_SECONDS, 3)
        scene["end"] = round(current + scene["duration"], 3)

        chunks = caption_chunks(scene["narration"])
        weights = [max(1, len(re.sub(r"\\s+", "", chunk))) for chunk in chunks]
        weight_total = sum(weights)
        cue_start = current
        for chunk, weight in zip(chunks, weights):
            cue_duration = spoken_duration * weight / weight_total
            cue_end = min(current + spoken_duration, cue_start + cue_duration)
            cues.append((cue_start, cue_end, chunk))
            cue_start = cue_end

        audio_parts.extend([samples, silence])
        current = scene["end"]
        script_lines.extend([f"{scene['id']} · {scene['title']}", scene["narration"], ""])

    narration = np.concatenate(audio_parts)
    sf.write(ASSETS / "narration.wav", narration, RATE)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(ASSETS / "narration.wav"),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(ASSETS / "narration.mp3"),
        ],
        check=True,
    )

    (ROOT / "scenes-timed.json").write_text(
        json.dumps(scenes, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (ROOT / "script.txt").write_text("\n".join(script_lines), encoding="utf-8")

    vtt = ["WEBVTT", ""]
    previous_end = 0.0
    for cue_start, cue_end, chunk in cues:
        cue_start = max(previous_end, cue_start)
        cue_end = max(cue_start + 0.08, cue_end)
        vtt.extend([f"{timestamp(cue_start)} --> {timestamp(cue_end)}", chunk, ""])
        previous_end = cue_end
    (ASSETS / "captions.en.vtt").write_text("\n".join(vtt), encoding="utf-8")
    print(json.dumps({"durationSeconds": round(current, 3), "scenes": len(scenes)}))


if __name__ == "__main__":
    main()
