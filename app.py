from flask import Flask, render_template, request, redirect, url_for, make_response, session
import sqlite3
import re
import os
import json
import io
import subprocess
import tempfile
import whisper
from groq import Groq
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import inch
from reportlab.lib import colors

app = Flask(__name__)
app.secret_key = "lume_secret_key_2026"

# ─── GROQ SETUP ───────────────────────────────────────────────────────────────
GROQ_API_KEY = "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"
groq_client = Groq(api_key=GROQ_API_KEY)

print("Loading Whisper model...")
whisper_model = whisper.load_model("tiny")
print("Whisper model loaded!")

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def extract_video_id(url):
    match = re.search(r"(?:v=|youtu\.be/)([\w-]+)", url)
    return match.group(1) if match else ""

def init_db():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_url TEXT, transcript TEXT, summary TEXT,
        key_points TEXT, quiz TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_url TEXT, summary TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

def download_audio(youtube_url):
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "audio.mp3")
    cmd = [
        "yt-dlp", "-x",
        "--audio-format", "mp3",
        "--audio-quality", "5",
        "--no-playlist",
        "-o", output_path,
        youtube_url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise Exception(f"yt-dlp failed: {result.stderr}")
    return output_path

def transcribe_audio(audio_path):
    result = whisper_model.transcribe(audio_path, fp16=False)
    return result["text"].strip()

def generate_summary_and_points(transcript):
    prompt = f"""You are an AI that helps students learn from YouTube videos.
Given this transcript, provide:
1. A clear, concise summary (3-5 sentences)
2. Exactly 5 key points as a JSON list

Respond ONLY in this exact JSON format:
{{
  "summary": "Your summary here...",
  "key_points": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5"]
}}

Transcript:
{transcript[:6000]}"""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1000
    )
    text = response.choices[0].message.content.strip()
    text = re.sub(r"```json|```", "", text).strip()
    data = json.loads(text)
    return data["summary"], data["key_points"]

def generate_quiz(transcript, summary):
    prompt = f"""Based on this video transcript and summary, generate exactly 5 multiple choice quiz questions.
Each question must have exactly 4 options and one correct answer.

Respond ONLY in this exact JSON format (no extra text):
[
  {{
    "question": "Question text here?",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "answer": "The correct option text exactly as written above"
  }}
]

Summary: {summary}
Transcript excerpt: {transcript[:3000]}"""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1500
    )
    text = response.choices[0].message.content.strip()
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/summarize", methods=["POST"])
def summarize():
    youtube_link = request.form.get("youtube_link", "").strip()
    if not youtube_link:
        return render_template("home.html", error="Please enter a YouTube link.")
    video_id = extract_video_id(youtube_link)
    if not video_id:
        return render_template("home.html", error="Invalid YouTube URL. Please try again.")
    audio_path = None
    try:
        print(f"[1/3] Downloading audio from: {youtube_link}")
        audio_path = download_audio(youtube_link)
        print("[2/3] Transcribing audio with Whisper...")
        transcript = transcribe_audio(audio_path)
        if not transcript:
            raise Exception("Could not transcribe audio.")
        print("[3/3] Generating summary and quiz with Groq...")
        summary, key_points = generate_summary_and_points(transcript)
        quiz_questions = generate_quiz(transcript, summary)
        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("""INSERT INTO summaries (video_url, transcript, summary, key_points, quiz)
                     VALUES (?, ?, ?, ?, ?)""",
                  (youtube_link, transcript, summary,
                   json.dumps(key_points), json.dumps(quiz_questions)))
        conn.commit()
        conn.close()
        session["quiz"] = quiz_questions
        session["summary"] = summary
        session["video_id"] = video_id
        session["key_points"] = key_points
        session["youtube_link"] = youtube_link
        return redirect(url_for("result"))
    except subprocess.TimeoutExpired:
        return render_template("home.html", error="Download timed out. Try a shorter video.")
    except Exception as e:
        print(f"Error: {e}")
        return render_template("home.html", error=f"Error processing video: {str(e)}")
    finally:
        if audio_path and os.path.exists(audio_path):
            try: os.remove(audio_path)
            except: pass

@app.route("/quiz")
def quiz():
    quiz_data = session.get("quiz")
    if not quiz_data:
        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("SELECT quiz FROM summaries WHERE quiz IS NOT NULL ORDER BY created_at DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        quiz_data = json.loads(row[0]) if row and row[0] else []
    return render_template("quiz.html", quiz=list(enumerate(quiz_data)))

@app.route("/submit_quiz", methods=["POST"])
def submit_quiz():
    quiz_data = session.get("quiz", [])
    score = 0
    results = []
    for i, q in enumerate(quiz_data):
        user_ans = request.form.get(f"q{i}")
        correct_ans = q["answer"]
        is_correct = user_ans == correct_ans
        if is_correct:
            score += 1
        results.append({"question": q["question"], "selected": user_ans,
                         "correct": correct_ans, "is_correct": is_correct})
    return render_template("score.html", results=results, score=score, total=len(quiz_data))

@app.route("/save_note", methods=["POST"])
def save_note():
    video_url = request.form.get("video_url")
    summary = request.form.get("summary")
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("INSERT INTO notes (video_url, summary) VALUES (?, ?)", (video_url, summary))
    conn.commit()
    conn.close()
    return redirect(url_for("notes"))

@app.route("/notes")
def notes():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT id, video_url, summary, created_at FROM notes ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return render_template("notes.html", notes=[{"id": r[0], "video": r[1], "summary": r[2], "date": r[3]} for r in rows])

@app.route("/delete_note/<int:note_id>")
def delete_note(note_id):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("DELETE FROM notes WHERE id=?", (note_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("notes"))

@app.route("/history")
def history():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT video_url, summary, created_at FROM summaries ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    history = []
    for row in rows:
        vid_id = extract_video_id(row[0])
        history.append({"video": row[0], "summary": row[1], "date": row[2], "vid_id": vid_id})
    return render_template("history.html", history=history)

@app.route("/dashboard")
def dashboard():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM summaries")
    total_summaries = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notes")
    total_notes = c.fetchone()[0]
    c.execute("SELECT video_url, created_at FROM summaries ORDER BY created_at DESC LIMIT 5")
    rows = c.fetchall()
    conn.close()
    return render_template("dashboard.html", total_summaries=total_summaries,
                           total_notes=total_notes,
                           recent=[{"video": r[0], "date": r[1]} for r in rows])

@app.route("/progress")
def progress():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM summaries")
    total_videos = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notes")
    total_notes = c.fetchone()[0]
    c.execute("SELECT video_url, created_at FROM summaries ORDER BY created_at DESC LIMIT 10")
    rows = c.fetchall()
    c.execute("SELECT video_url, created_at FROM notes ORDER BY created_at DESC LIMIT 10")
    note_rows = c.fetchall()
    conn.close()
    recent = [{"video": r[0], "date": r[1], "vid_id": extract_video_id(r[0])} for r in rows]
    notes_list = [{"video": r[0], "date": r[1], "vid_id": extract_video_id(r[0])} for r in note_rows]
    streak = min(total_videos, 7)
    best_score = 100 if total_videos > 0 else 0
    return render_template("progress.html", total_videos=total_videos,
                           total_notes=total_notes, best_score=best_score,
                           streak=streak, recent=recent, notes_list=notes_list)

@app.route("/download_pdf", methods=["POST"])
def download_pdf():
    video_url = request.form.get("video_url", "")
    summary = request.form.get("summary", "")
    key_points_raw = request.form.getlist("key_points")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=inch, leftMargin=inch,
                            topMargin=inch, bottomMargin=inch)
    title_style = ParagraphStyle('Title', fontSize=20, fontName='Helvetica-Bold',
                                  textColor=colors.HexColor('#ff8a00'), spaceAfter=20)
    heading_style = ParagraphStyle('Heading', fontSize=14, fontName='Helvetica-Bold',
                                    textColor=colors.HexColor('#e52e71'), spaceAfter=10, spaceBefore=15)
    body_style = ParagraphStyle('Body', fontSize=11, fontName='Helvetica',
                                 textColor=colors.black, spaceAfter=8, leading=16)
    url_style = ParagraphStyle('URL', fontSize=10, fontName='Helvetica',
                                textColor=colors.grey, spaceAfter=20)
    story = [
        Paragraph("AI Video Summary", title_style),
        Paragraph(f"Video: {video_url}", url_style),
        Spacer(1, 10),
        Paragraph("Summary", heading_style),
        Paragraph(summary, body_style),
        Spacer(1, 10),
    ]
    if key_points_raw:
        story.append(Paragraph("Key Points", heading_style))
        for point in key_points_raw:
            story.append(Paragraph(f"• {point}", body_style))
    doc.build(story)
    buffer.seek(0)
    response = make_response(buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = 'attachment; filename=summary.pdf'
    return response

@app.route("/result")
def result():
    summary = session.get("summary")
    video_id = session.get("video_id")
    key_points = session.get("key_points")
    youtube_link = session.get("youtube_link")
    if not summary:
        return redirect(url_for("home"))
    return render_template("summary.html", summary=summary, video_id=video_id,
                           key_points=key_points, youtube_link=youtube_link)

if __name__ == "__main__":
    app.run(debug=True)