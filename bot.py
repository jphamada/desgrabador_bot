import os
import re
import tempfile
import asyncio
from dotenv import load_dotenv

# Inyecta los binarios estáticos de ffmpeg y ffprobe en el PATH
import static_ffmpeg
static_ffmpeg.add_paths()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("Falta la variable de entorno TELEGRAM_BOT_TOKEN.")
if not GEMINI_API_KEY:
    raise ValueError("Falta la variable de entorno GEMINI_API_KEY.")

# Inicializamos cliente de Gemini
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Memoria temporal por usuario: {user_id: {"type": str, "content": str, "path": str}}
USER_CONTEXT = {}

PROMPTS = {
    "nota": (
        "Actúa como un editor y redactor periodístico de primer nivel. "
        "A partir de este material, redacta una nota periodística completa con estructura de pirámide invertida: "
        "1) Título atractivo y riguroso.\n"
        "2) Bajada explicativa.\n"
        "3) Cuerpo de la nota organizado con subtítulos y citas textuales relevantes entrecomilladas con su contexto.\n"
        "Usa español rioplatense neutro y profesional."
    ),
    "transcripcion": (
        "Limpia y transcribe el siguiente material de audio/video de forma fiel y legible. "
        "Elimina muletillas innecesarias ('eh', 'este', repeticiones vacías), "
        "organiza en párrafos temáticos legibles y conserva los nombres propios y cifras con máxima precisión."
    ),
    "citas": (
        "Extrae las 4 a 6 declaraciones o citas más impactantes, polémicas o informativamente valiosas de este material. "
        "Presenta cada una entre comillas, indicando el contexto o tema de la declaración, lista para usar en placas de redes."
    ),
    "minuta": (
        "Elabora una minuta ejecutiva formal a partir de este material:\n"
        "- Tema principal y objetivo.\n"
        "- Puntos clave tratados (en viñetas claras).\n"
        "- Acuerdos, compromisos o próximos pasos.\n"
        "Tono profesional y directo."
    ),
}

def extract_youtube_id(url: str):
    regex = r"(?:v=|\/|youtu\.be\/|embed\/)([0-9A-Za-z_-]{11})"
    match = re.search(regex, url)
    return match.group(1) if match else None

def get_youtube_transcript(video_id: str):
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None

        # 1. Subtítulos manuales o directos en español o inglés
        try:
            transcript = transcript_list.find_transcript(['es', 'es-419', 'es-AR', 'es-ES', 'es-US', 'en'])
        except Exception:
            pass

        # 2. Subtítulos automáticos
        if not transcript:
            try:
                transcript = transcript_list.find_generated_transcript(['es', 'es-419', 'es-AR', 'es-ES', 'en'])
            except Exception:
                pass

        # 3. Fallback: traducir al español el primer idioma disponible
        if not transcript:
            try:
                for t in transcript_list:
                    transcript = t.translate('es')
                    break
            except Exception:
                pass

        if transcript:
            data = transcript.fetch()
            full_text = " ".join([item['text'].strip() for item in data if item.get('text')])
            return full_text

        return None
    except Exception as e:
        print(f"Aviso al extraer subtítulos de YouTube: {e}")
        return None

def download_youtube_audio(url: str, output_path: str):
    ydl_opts = {
        'format': 'ba/b',
        'outtmpl': f"{output_path}.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'fixup': 'never',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
    return filename

def build_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📝 Nota periodística", callback_data="nota"),
            InlineKeyboardButton("⏱️ Transcripción limpia", callback_data="transcripcion"),
        ],
        [
            InlineKeyboardButton("📌 Citas destacadas", callback_data="citas"),
            InlineKeyboardButton("💼 Minuta ejecutiva", callback_data="minuta"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Mandame un enlace de YouTube, una nota de voz o un archivo de audio.\n\n"
        "Podrás generar notas periodísticas, transcripciones limpias, citas para placas o minutas ejecutivas."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message

    status_msg = await message.reply_text("⏳ Procesando material...")

    try:
        # Caso 1: Enlace de YouTube
        if message.text and ("youtube.com" in message.text or "youtu.be" in message.text):
            video_id = extract_youtube_id(message.text)
            if not video_id:
                await status_msg.edit_text("❌ No pude reconocer el enlace de YouTube.")
                return

            await status_msg.edit_text("🔍 Buscando transcripción en YouTube...")
            transcript_text = get_youtube_transcript(video_id)

            if transcript_text:
                USER_CONTEXT[user_id] = {"type": "text", "content": transcript_text}
                await status_msg.edit_text(
                    "✅ Transcripción extraída con éxito.\n¿Qué querés generar?",
                    reply_markup=build_keyboard()
                )
            else:
                await status_msg.edit_text("⬇️ Sin subtítulos disponibles. Descargando audio del video...")
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp_base = tmp.name

                audio_file = download_youtube_audio(message.text, tmp_base)
                USER_CONTEXT[user_id] = {"type": "audio_file", "path": audio_file}
                await status_msg.edit_text(
                    "✅ Audio descargado y listo.\n¿Qué querés generar?",
                    reply_markup=build_keyboard()
                )

        # Caso 2: Nota de voz o audio
        elif message.voice or message.audio: