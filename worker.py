

import os
import tempfile
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "generated-videos"

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY
)


def get_next_job():
    result = (
        supabase.table("video_jobs")
        .select("*")
        .eq("status", "queued")
        .order("id")
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def claim_job(job_id):
    result = (
        supabase.table("video_jobs")
        .update({"status": "processing"})
        .eq("id", job_id)
        .eq("status", "queued")
        .execute()
    )
    return bool(result.data)


def get_job_items(job_id):
    result = (
        supabase.table("video_job_items")
        .select("question_no,mcq_id")
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
            "correct_option,explanation"
        )
        .eq("id", mcq_id)
        .eq("active", True)
        .eq("verification_status", "verified")
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None

def load_font(size, bold=False):
    filename = (
        "DejaVuSans-Bold.ttf" if bold
        else "DejaVuSans.ttf"
    )
    path = "/usr/share/fonts/truetype/dejavu/" + filename

    if os.path.exists(path):
        return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):
    words = str(text or "").split()
    lines = []
    current = ""

    for word in words:
        test = (current + " " + word).strip()

        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines or [""]


def draw_wrapped(
    draw, text, x, y, font, max_width,
    fill="white", gap=8
):
    for line in wrap_text(draw, text, font, max_width):
        draw.text((x, y), line, font=font, fill=fill)
        box = draw.textbbox((0, 0), line, font=font)
        y += box[3] + gap

    return y


def create_slide(mcq, number, total, path, width, height):
    image = Image.new("RGB", (width, height), "#101827")
    draw = ImageDraw.Draw(image)

    margin = int(width * 0.07)
    max_width = width - 2 * margin

    title_font = load_font(max(20, width // 30), True)
    question_font = load_font(max(22, width // 34), True)
    option_font = load_font(max(18, width // 42))
    answer_font = load_font(max(19, width // 38), True)

    y = int(height * 0.06)

    draw.text(
        (margin, y),
        f"CSS COMPASS | MCQ {number}/{total}",
        font=title_font,
        fill="#66D9EF"
    )

    y += int(height * 0.10)

    y = draw_wrapped(
        draw, mcq.get("question", ""),
        margin, y, question_font, max_width
    )

    y += int(height * 0.035)

    for letter, key in [
        ("A", "option_a"),
        ("B", "option_b"),
        ("C", "option_c"),
        ("D", "option_d")
    ]:
        y = draw_wrapped(
            draw,
            f"{letter}. {mcq.get(key, '')}",
            margin, y, option_font, max_width
        )
        y += int(height * 0.018)

    y += int(height * 0.02)

    answer = str(mcq.get("correct_option") or "").strip().upper()

    draw_wrapped(
        draw, f"Correct answer: {answer}",
        margin, y, answer_font, max_width,
        fill="#7CFC98"
    )

    image.save(path, "PNG")
          
def render_video(job, mcqs, output_path):
    duration = int(job["duration_seconds"])

    if not 1 <= duration <= 1200:
        raise RuntimeError("Duration must be 1 to 1200 seconds.")

    if not 1 <= len(mcqs) <= 50:
        raise RuntimeError("Video must contain 1 to 50 MCQs.")

    if job["aspect_ratio"] == "9:16":
        width, height = 720, 1280
    elif job["aspect_ratio"] == "16:9":
        width, height = 1280, 720
    else:
        raise RuntimeError("Unsupported aspect ratio.")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        slides = []

        for index, mcq in enumerate(mcqs, start=1):
            slide = temp / f"slide_{index:03d}.png"

            create_slide(
                mcq, index, len(mcqs),
                str(slide), width, height
            )

            slides.append(slide)

        seconds_per_slide = duration / len(slides)
        concat_file = temp / "slides.txt"

        with open(concat_file, "w", encoding="utf-8") as file:
            for slide in slides:
                safe_path = str(slide).replace("\\", "/")
                file.write(f"file '{safe_path}'\n")
                file.write(f"duration {seconds_per_slide:.6f}\n")

            safe_path = str(slides[-1]).replace("\\", "/")
            file.write(f"file '{safe_path}'\n")

        command = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-vf", "fps=25,format=yuv420p",
            "-t", str(duration),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-movflags", "+faststart",
            output_path
        ]

        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True
        )
          
def process_job(job):
    job_id = job["id"]

    if not claim_job(job_id):
        print(f"Job {job_id} was already claimed.")
        return

    try:
        items = get_job_items(job_id)

        if not items or len(items) > 50:
            raise RuntimeError(
                "Job must contain 1 to 50 attached MCQs."
            )

        mcqs = []

        for item in items:
            mcq = get_mcq(item["mcq_id"])

            if mcq is None:
                raise RuntimeError(
                    f"Active verified MCQ not found: {item['mcq_id']}"
                )

            answer = str(
                mcq.get("correct_option") or ""
            ).strip().upper()

            if answer not in {"A", "B", "C", "D"}:
                raise RuntimeError(
                    f"MCQ {item['mcq_id']} has an invalid answer."
                )

            mcqs.append(mcq)

        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(
                temp_dir, f"css_compass_job_{job_id}.mp4"
            )

            render_video(job, mcqs, output)
            storage_path = f"generated/job_{job_id}.mp4"

            with open(output, "rb") as video_file:
                supabase.storage.from_(BUCKET).upload(
                    storage_path,
                    video_file,
                    {
                        "content-type": "video/mp4",
                        "upsert": "true"
                    }
                )

        supabase.table("generated_videos").insert({
            "job_id": job_id,
            "storage_path": storage_path,
            "duration_seconds": job["duration_seconds"]
        }).execute()

        supabase.table("video_jobs").update({
            "status": "completed"
        }).eq("id", job_id).execute()

        print(f"Job {job_id} completed successfully.")

    except Exception as error:
        print(f"Job {job_id} failed: {error}")

        try:
            supabase.table("video_jobs").update({
                "status": "failed"
            }).eq("id", job_id).execute()
        except Exception as update_error:
            print(f"Could not update failed status: {update_error}")


def main():
    job = get_next_job()

    if not job:
        print("No queued video jobs.")
        return

    process_job(job)


if __name__ == "__main__":
    main()
      
