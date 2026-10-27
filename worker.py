
import os
import re
import sys
import time
import shutil
import tempfile
import subprocess
import traceback
from pathlib import Path
from textwrap import wrap

from PIL import Image, ImageDraw, ImageFont
from supabase import create_client


# ---------- CONFIGURATION ----------

SUPABASE_URL = os.environ["SUPABASE_URL"].strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()

BUCKET = "generated-videos"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ---------- DATABASE ----------

def get_next_job():
    print("Checking Supabase connection...")
    print("Supabase project host:", SUPABASE_URL.split("//")[-1].split("/")[0])

    visible = (
        supabase.table("video_jobs")
        .select("id,status")
        .order("id")
        .limit(10)
        .execute()
    )
    print("Visible jobs:", visible.data)

    result = (
        supabase.table("video_jobs")
        .select("*")
        .eq("status", "queued")
        .order("id")
        .limit(1)
        .execute()
    )

    jobs = result.data or []
    print("Queued jobs found:", len(jobs))
    return jobs[0] if jobs else None


def claim_job(job_id):
    result = (
        supabase.table("video_jobs")
        .update({"status": "processing"})
        .eq("id", job_id)
        .eq("status", "queued")
        .select("id")
        .execute()
    )
    return bool(result.data)


def get_job_items(job_id):
    result = (
        supabase.table("video_job_items")
        .select("id,job_id,mcq_id,question_no")
        .eq("job_id", job_id)
        .order("question_no")
        .execute()
    )
    return result.data or []


def get_mcq(mcq_id):
    result = (
        supabase.table("mcqs")
        .select(
            "id,question,option_a,option_b,option_c,option_d,"
            "correct_option,explanation,verification_status,active"
        )
        .eq("id", mcq_id)
        .eq("active", True)
        .eq("verification_status", "verified")
        .limit(1)
        .execute()
    )

    rows = result.data or []
    if not rows:
        raise ValueError(
            f"MCQ {mcq_id} is missing, inactive, or not verified."
        )

    mcq = rows[0]
    if mcq.get("correct_option") not in ("A", "B", "C", "D"):
        raise ValueError(f"MCQ {mcq_id} has an invalid correct option.")

    return mcq


# ---------- VIDEO DESIGN ----------

def load_font(size, bold=False):
    path = BOLD_FONT_PATH if bold else FONT_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):
    words = str(text or "").split()
    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines or [""]


def draw_wrapped(draw, text, x, y, font, max_width,
                 fill="white", line_gap=12, max_lines=8):
    lines = wrap_text(draw, text, font, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "..."

    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        box = draw.textbbox((x, y), line, font=font)
        y += box[3] - box[1] + line_gap

    return y


def create_slide(mcq, number, total, width, height, output_path):
    image = Image.new("RGB", (width, height), "#101827")
    draw = ImageDraw.Draw(image)

    margin = int(width * 0.07)
    small = load_font(max(20, width // 42), True)
    question_font = load_font(max(25, width // 32), True)
    option_font = load_font(max(20, width // 42))
    answer_font = load_font(max(18, width // 48), True)

    draw.rectangle((0, 0, width, int(height * 0.018)), fill="#38bdf8")

    draw.text(
        (margin, int(height * 0.05)),
        "CSS COMPASS  |  MCQ PRACTICE",
        font=small,
        fill="#38bdf8"
    )
    draw.text(
        (margin, int(height * 0.11)),
        f"QUESTION {number}/{total}",
        font=small,
        fill="#fbbf24"
    )

    y = int(height * 0.19)
    y = draw_wrapped(
        draw, mcq.get("question", ""), margin, y,
        question_font, width - 2 * margin,
        line_gap=int(height * 0.012), max_lines=7
    )

    options = [
        ("A", mcq.get("option_a")),
        ("B", mcq.get("option_b")),
        ("C", mcq.get("option_c")),
        ("D", mcq.get("option_d")),
    ]

    y += int(height * 0.025)
    option_gap = int(height * 0.015)
    box_height = int(height * 0.105)
    correct = mcq["correct_option"]

    for letter, text in options:
        if y + box_height > int(height * 0.82):
            break

        is_correct = letter == correct
        fill = "#14532d" if is_correct else "#1e293b"
        outline = "#4ade80" if is_correct else "#475569"

        draw.rounded_rectangle(
            (margin, y, width - margin, y + box_height),
            radius=max(8, width // 80),
            fill=fill,
            outline=outline,
            width=3
        )

        draw.text(
            (margin + 18, y + 12),
            f"{letter}.",
            font=option_font,
            fill="#4ade80" if is_correct else "#fbbf24"
        )

        draw_wrapped(
            draw, text or "(option unavailable)",
            margin + int(width * 0.11), y + 12,
            option_font, width - 2 * margin - int(width * 0.14),
            line_gap=5, max_lines=2
        )
        y += box_height + option_gap

    footer_y = int(height * 0.88)
    draw.line(
        (margin, footer_y, width - margin, footer_y),
        fill="#475569", width=2
    )

    draw.text(
        (margin, footer_y + 15),
        f"Correct answer: {correct}",
        font=answer_font,
        fill="#4ade80"
    )

    explanation = (mcq.get("explanation") or "").strip()
    if explanation:
        draw_wrapped(
            draw, "Explanation: " + explanation,
            margin, footer_y + int(height * 0.065),
            load_font(max(15, width // 60)),
            width - 2 * margin,
            fill="#cbd5e1", line_gap=4, max_lines=2
        )

    image.save(output_path, format="PNG")


def render_video(job, mcqs, work_dir):
    aspect = job.get("aspect_ratio", "16:9")

    if aspect == "9:16":
        width, height = 720, 1280
    elif aspect == "16:9":
        width, height = 1280, 720
    else:
        raise ValueError(f"Unsupported aspect ratio: {aspect}")

    duration = int(job.get("duration_seconds") or 10)
    if not 1 <= duration <= 1200:
        raise ValueError("Video duration must be between 1 and 1200 seconds.")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed.")

    frames_dir = Path(work_dir) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    for index, mcq in enumerate(mcqs, start=1):
        frame = frames_dir / f"slide_{index:04d}.png"
        create_slide(mcq, index, len(mcqs), width, height, frame)

    # Divide the requested duration across all slides.
    seconds_per_slide = duration / len(mcqs)
    concat_file = Path(work_dir) / "slides.txt"

    with concat_file.open("w", encoding="utf-8") as file:
        for index in range(1, len(mcqs) + 1):
            frame_path = (frames_dir / f"slide_{index:04d}.png").resolve()
            escaped = str(frame_path).replace("'", "'\\''")
            file.write(f"file '{escaped}'\n")
            file.write(f"duration {seconds_per_slide:.6f}\n")

        # Repeat final frame so FFmpeg honors its duration.
        final_path = (frames_dir / f"slide_{len(mcqs):04d}.png").resolve()
        escaped = str(final_path).replace("'", "'\\''")
        file.write(f"file '{escaped}'\n")

    output_path = Path(work_dir) / f"job_{job['id']}.mp4"

    command = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-vf", f"scale={width}:{height},format=yuv420p",
        "-r", "24",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-t", str(duration),
        str(output_path)
    ]

    subprocess.run(command, check=True, capture_output=True, text=True)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg did not produce a video file.")

    return output_path, duration


# ---------- PROCESS JOB ----------

def process_job(job):
    job_id = job["id"]
    print(f"Starting video job {job_id}")

    try:
        items = get_job_items(job_id)
        if not items:
            raise ValueError(f"Job {job_id} has no linked MCQs.")

        expected_count = int(job.get("item_count") or len(items))
        if len(items) < expected_count:
            print(
                f"Warning: job requested {expected_count} MCQs, "
                f"but only {len(items)} are linked."
            )

        mcqs = []
        for item in items:
            mcqs.append(get_mcq(item["mcq_id"]))

        with tempfile.TemporaryDirectory(prefix="css_compass_") as work_dir:
            video_path, duration = render_video(job, mcqs, work_dir)

            storage_path = f"generated/job_{job_id}.mp4"
            with video_path.open("rb") as video_file:
                supabase.storage.from_(BUCKET).upload(
                    storage_path,
                    video_file.read(),
                    file_options={
                        "content-type": "video/mp4",
                        "upsert": "true"
                    }
                )

        # Save the generated video's database record.
        supabase.table("generated_videos").insert({
            "job_id": job_id,
            "storage_path": storage_path,
            "duration_seconds": duration
        }).execute()

        supabase.table("video_jobs").update({
            "status": "completed"
        }).eq("id", job_id).execute()

        print(f"Job {job_id} completed successfully.")
        print(f"Storage path: {storage_path}")

    except Exception as error:
        print(f"Job {job_id} failed: {error}")
        traceback.print_exc()

        try:
            supabase.table("video_jobs").update({
                "status": "failed"
            }).eq("id", job_id).execute()
        except Exception as update_error:
            print("Could not update failed status:", update_error)

        raise


def main():
    print("CSS Compass Video Worker starting.")

    try:
        job = get_next_job()
    except Exception as error:
        print("Could not read video_jobs from Supabase:", error)
        traceback.print_exc()
        sys.exit(1)

    if not job:
        print("No queued video jobs.")
        return

    if not claim_job(job["id"]):
        print(f"Job {job['id']} was already claimed or changed.")
        return

    process_job(job)


if __name__ == "__main__":
    main()
