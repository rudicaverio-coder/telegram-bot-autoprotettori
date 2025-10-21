# bot_cambi_webhook.py
import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from datetime import datetime, timedelta
import asyncio
import os
from flask import Flask, request
import threading
import requests
import time
import psutil
import base64
import json
from typing import Dict, List, Tuple

# === CONFIGURAZIONE ===
BOT_TOKEN_CAMBI = os.environ.get('BOT_TOKEN_CAMBI')
DATABASE_CAMBI = 'cambi_vvf.db'
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GIST_ID_CAMBI = os.environ.get('GIST_ID_CAMBI')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://telegram-bot-cambi.onrender.com')
WEBHOOK_PORT = 10001

# ID unico utilizzatore
MY_USER_ID = 1816045269

# Configurazione logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === DATABASE SCHEMA COMPLETO ===
def init_db_cambi():
    """Inizializzazione database completo per gestione cambi e squadre"""
    conn = sqlite3.connect(DATABASE_CAMBI)
    c = conn.cursor()
    
    # Tabella VVF
    c.execute('''
        CREATE TABLE IF NOT EXISTS vvf (
            id INTEGER PRIMARY KEY,
            user_id INTEGER UNIQUE,
            qualifica TEXT CHECK(qualifica IN ('VV', 'CSV')),
            cognome TEXT,
            nome TEXT,
            autista TEXT CHECK(autista IN ('I', 'II', 'III')),
            data_inserimento TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabella Tipologie Turno
    c.execute('''
        CREATE TABLE IF NOT EXISTS tipologie_turno (
            id INTEGER PRIMARY KEY,
            nome TEXT UNIQUE,
            ore_base REAL,
            descrizione TEXT
        )
    ''')
    
    # Tabella Cambi
    c.execute('''
        CREATE TABLE IF NOT EXISTS cambi (
            id INTEGER PRIMARY KEY,
            data_cambio DATE,
            tipo_operazione TEXT CHECK(tipo_operazione IN ('dato', 'ricevuto')),
            vvf_da_id INTEGER,
            vvf_a_id INTEGER,
            tipologia_turno_id INTEGER,
            ore_effettive REAL,
            note TEXT,
            stato TEXT DEFAULT 'programmato' CHECK(stato IN ('programmato', 'effettuato', 'cancellato')),
            data_inserimento TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (vvf_da_id) REFERENCES vvf(id),
            FOREIGN KEY (vvf_a_id) REFERENCES vvf(id),
            FOREIGN KEY (tipologia_turno_id) REFERENCES tipologie_turno(id)
        )
    ''')
    
    # Tabella per sistema squadre
    c.execute('''
        CREATE TABLE IF NOT EXISTS tipi_squadra (
            id INTEGER PRIMARY KEY,
            nome TEXT UNIQUE,
            descrizione TEXT,
            numero_squadre INTEGER
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS squadre (
            id INTEGER PRIMARY KEY,
            tipo_squadra_id INTEGER,
            nome TEXT,
            ordine INTEGER,
            FOREIGN KEY (tipo_squadra_id) REFERENCES tipi_squadra(id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS squadre_componenti (
            id INTEGER PRIMARY KEY,
            squadra_id INTEGER,
            vvf_id INTEGER,
            FOREIGN KEY (squadra_id) REFERENCES squadre(id),
            FOREIGN KEY (vvf_id) REFERENCES vvf(id),
            UNIQUE(squadra_id, vvf_id)
        )
    ''')
    
    # Inserimento dati default
    tipologie_standard = [
        ('notte_completa', 7.0, 'Turno notte completo 24-07'),
        ('festivo', 13.0, 'Turno festivo 07-20'),
        ('weekend', 32.0, 'Weekend completo Sab-Dom'),
        ('sera_feriale', 4.0, 'Sera feriale 20-24'),
        ('parziale', 0.0, 'Turno parziale ore variabili')
    ]
    
    c.executemany('''
        INSERT OR IGNORE INTO tipologie_turno (nome, ore_base, descrizione)
        VALUES (?, ?, ?)
    ''', tipologie_standard)
    
    # Inserimento tipi squadra predefiniti
    tipi_squadra = [
        ('Squadre Weekend', 'Squadre ABCD per weekend', 4),
        ('Squadre Notti Feriali', 'Squadre An Bn Cn per notti feriali', 3),
        ('Squadre Notti Venerdì', 'Squadre S1n S2n per notti venerdì', 2),
        ('Squadre Sere', 'Squadre S1-S7 per sere feriali', 7)
    ]
    
    c.executemany('''
        INSERT OR IGNORE INTO tipi_squadra (nome, descrizione, numero_squadre)
        VALUES (?, ?, ?)
    ''', tipi_squadra)
    
    # Inserimento squadre predefinite
    squadre_predefinite = [
        (1, 'A', 1), (1, 'B', 2), (1, 'C', 3), (1, 'D', 4),
        (2, 'An', 1), (2, 'Bn', 2), (2, 'Cn', 3),
        (3, 'S1n', 1), (3, 'S2n', 2),
        (4, 'S1', 1), (4, 'S2', 2), (4, 'S3', 3), 
        (4, 'S4', 4), (4, 'S5', 5), (4, 'S6', 6), (4, 'S7', 7)
    ]
    
    c.executemany('''
        INSERT OR IGNORE INTO squadre (tipo_squadra_id, nome, ordine)
        VALUES (?, ?, ?)
    ''', squadre_predefinite)
    
    conn.commit()
    conn.close()

init_db_cambi()

# === FUNZIONI UTILITY DATABASE ===
def get_conn():
    return sqlite3.connect(DATABASE_CAMBI)

# === SISTEMA "CHI TOCCA" - CALENDARIO INTELLIGENTE ===
def calcola_squadra_di_turno(tipo_squadra: str, data: datetime) -> str:
    """Calcola quale squadra è di turno in base a data e tipo"""
    conn = get_conn()
    c = conn.cursor()
    
    c.execute('SELECT id, numero_squadre FROM tipi_squadra WHERE nome = ?', (tipo_squadra,))
    tipo = c.fetchone()
    if not tipo:
        conn.close()
        return "N/D"
    
    tipo_id, numero_squadre = tipo
    
    c.execute('SELECT id, nome FROM squadre WHERE tipo_squadra_id = ? ORDER BY ordine', (tipo_id,))
    squadre = c.fetchall()
    
    # Logica di rotazione
    if tipo_squadra == "Squadre Weekend":
        inizio_anno = datetime(data.year, 1, 1)
        giorni_dall_inizio = (data - inizio_anno).days
        settimana = giorni_dall_inizio // 7
        indice = settimana % numero_squadre
        squadra = squadre[indice][1]
        
    elif tipo_squadra == "Squadre Notti Feriali":
        inizio_settimana = data - timedelta(days=data.weekday())
        giorni_dalla_domenica = (data - inizio_settimana).days
        indice = giorni_dalla_domenica % numero_squadre
        squadra = squadre[indice][1]
        
    elif tipo_squadra == "Squadre Notti Venerdì":
        inizio_anno = datetime(data.year, 1, 1)
        settimane_dall_inizio = (data - inizio_anno).days // 7
        indice = (settimane_dall_inizio // 2) % numero_squadre
        squadra = squadre[indice][1]
        
    elif tipo_squadra == "Squadre Sere":
        inizio_anno = datetime(data.year, 1, 1)
        giorni_dall_inizio = (data - inizio_anno).days
        indice = giorni_dall_inizio % numero_squadre
        squadra = squadre[indice][1]
    
    else:
        squadra = "N/D"
    
    conn.close()
    return squadra

def e_festivo(data: datetime) -> bool:
    """Verifica se una data è festiva"""
    return data.weekday() == 6  # Domenica

def get_chi_tocca_oggi() -> str:
    """Calcola chi tocca oggi per tutti i turni"""
    oggi = datetime.now()
    
    turni_oggi = []
    
    # SERA (oggi 20-24)
    if oggi.hour < 20:
        if not e_festivo(oggi) and oggi.weekday() != 5:  # Non festivo e non sabato
            squadra_sera = calcola_squadra_di_turno("Squadre Sere", oggi)
            turni_oggi.append(f"🌙 **Sera oggi (20-24):** {squadra_sera}")
    
    # NOTTE (stasera -> domani 24-07)
    if oggi.weekday() == 4:  # Venerdì
        squadra_notte = calcola_squadra_di_turno("Squadre Notti Venerdì", oggi)
    elif 0 <= oggi.weekday() <= 3:  # Lun-Gio
        squadra_notte = calcola_squadra_di_turno("Squadre Notti Feriali", oggi)
    else:
        squadra_notte = "Weekend"
    
    if squadra_notte != "Weekend":
        turni_oggi.append(f"🌃 **Notte stasera (24-07):** {squadra_notte}")
    
    # WEEKEND
    if oggi.weekday() >= 5 or e_festivo(oggi):
        squadra_weekend = calcola_squadra_di_turno("Squadre Weekend", oggi)
        turni_oggi.append(f"🎯 **Weekend/Festivo:** {squadra_weekend}")
    
    if turni_oggi:
        messaggio = "📅 **CHI TOCCA OGGI**\n\n" + "\n".join(turni_oggi)
    else:
        messaggio = "📅 Oggi non ci sono turni programmati"
    
    messaggio += f"\n\n👤 **Le tue squadre:**\n• Weekend: D\n• Notti feriali: Bn\n• Sere: S7"
    
    return messaggio

# === TASTIERA FISICA ===
def crea_tastiera_cambi(user_id: int) -> ReplyKeyboardMarkup:
    """Crea la tastiera fisica completa"""
    if user_id != MY_USER_ID:
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Accesso Negato")]], resize_keyboard=True)
    
    tastiera = [
        [KeyboardButton("👥 Gestisci VVF"), KeyboardButton("📊 Stato Singolo")],
        [KeyboardButton("🔄 Aggiungi Cambio"), KeyboardButton("🗑️ Rimuovi Cambio")],
        [KeyboardButton("📈 Prospetto Totale"), KeyboardButton("⏰ Carichi Pendenti")],
        [KeyboardButton("🔔 Mie Sostituzioni"), KeyboardButton("📅 Chi Tocca")],
        [KeyboardButton("🏃‍♂️ Gestisci Squadre"), KeyboardButton("🆘 Help Cambi")]
    ]
    
    return ReplyKeyboardMarkup(tastiera, resize_keyboard=True, is_persistent=True)

# === HANDLER PRINCIPALI ===
async def start_cambi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando start per bot cambi"""
    user_id = update.effective_user.id
    
    if user_id != MY_USER_ID:
        await update.message.reply_text("❌ Accesso riservato.")
        return
    
    welcome_text = """
🤖 **BENVENUTO NEL BOT GESTIONE CAMBI VVF!**

🎯 **FUNZIONALITÀ PRINCIPALI:**

📋 **GESTIONE CAMBI:**
• 👥 Gestisci lista VVF
• 📊 Visualizza stato singolo con bilancio ore
• 🔄 Aggiungi nuovi cambi (dati/ricevuti)
• 🗑️ Rimuovi cambi errati
• 📈 Prospetto completo di tutti i VVF
• ⏰ Carichi pendenti programmati
• 🔔 Mie sostituzioni future

📅 **SISTEMA SQUADRE:**
• 📅 Chi tocca oggi/domani
• 🏃‍♂️ Gestione completa squadre
• 👥 Assegnazione componenti alle squadre
• 🎯 Rotazione automatica turni

⚙️ **Sistema sempre attivo con backup automatico!**
"""
    
    await update.message.reply_text(welcome_text, reply_markup=crea_tastiera_cambi(user_id))

async def handle_message_cambi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce tutti i messaggi di testo"""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id != MY_USER_ID:
        await update.message.reply_text("❌ Accesso riservato.")
        return

    # ROUTING DEI COMANDI
    if text == "📅 Chi Tocca":
        messaggio_chi_tocca = get_chi_tocca_oggi()
        await update.message.reply_text(messaggio_chi_tocca)
        
    elif text == "🏃‍♂️ Gestisci Squadre":
        await mostra_gestione_squadre(update, context)
        
    elif text == "👥 Gestisci VVF":
        await mostra_gestione_vvf(update, context)
        
    elif text == "📊 Stato Singolo":
        await update.message.reply_text("🔧 Funzione in sviluppo...")
        
    elif text == "🔄 Aggiungi Cambio":
        await avvia_wizard_cambio(update, context)
        
    elif text == "🆘 Help Cambi":
        await help_cambi(update, context)
        
    else:
        await update.message.reply_text("ℹ️ Usa i pulsanti per navigare.", 
                                      reply_markup=crea_tastiera_cambi(user_id))

# === GESTIONE SQUADRE ===
async def mostra_gestione_squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu principale gestione squadre"""
    keyboard = [
        [InlineKeyboardButton("👀 Visualizza Squadre", callback_data="squadre_visualizza")],
        [InlineKeyboardButton("➕ Aggiungi Componente", callback_data="squadre_aggiungi_componente")],
        [InlineKeyboardButton("📅 Chi Tocca Domani", callback_data="squadre_domani")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(
            "🏃‍♂️ **GESTIONE SQUADRE**\n\nScegli un'operazione:",
            reply_markup=reply_markup
        )
    else:
        await update.callback_query.edit_message_text(
            "🏃‍♂️ **GESTIONE SQUADRE**\n\nScegli un'operazione:",
            reply_markup=reply_markup
        )

async def mostra_gestione_vvf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu gestione VVF"""
    keyboard = [
        [InlineKeyboardButton("➕ Aggiungi VVF", callback_data="vvf_aggiungi")],
        [InlineKeyboardButton("👀 Visualizza Tutti", callback_data="vvf_visualizza")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text("👥 **GESTIONE VVF**\n\nScegli un'operazione:", reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text("👥 **GESTIONE VVF**\n\nScegli un'operazione:", reply_markup=reply_markup)

async def avvia_wizard_cambio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avvia il wizard per aggiungere un cambio"""
    oggi = datetime.now()
    keyboard = []
    
    for i in range(7):
        data = oggi + timedelta(days=i)
        keyboard.append([InlineKeyboardButton(
            data.strftime("%d/%m (%a)"),
            callback_data=f"cambio_data_{data.strftime('%Y-%m-%d')}"
        )])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📅 **Seleziona data del cambio:**", reply_markup=reply_markup)

async def help_cambi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Messaggio di help completo"""
    help_text = """
🆘 **GUIDA BOT GESTIONE CAMBI VVF**

📋 **GESTIONE VVF:**
• **Aggiungi VVF:** Inserisci nuovi volontari (VV/CSV) con qualifica autista
• **Visualizza Tutti:** Vedi l'elenco completo

📊 **STATO E BILANCI:**
• **Stato Singolo:** Bilancio ore dettagliato per ogni VVF
• **Prospetto Totale:** Panoramica di tutti i bilanci
• **Carichi Pendenti:** Cambi programmati ma non effettuati

🔄 **GESTIONE CAMBI:**
• **Aggiungi Cambio:** Wizard guidato per inserire cambi
• **Rimuovi Cambio:** Cancella cambi inseriti per errore

📅 **SISTEMA SQUADRE:**
• **Chi Tocca:** Visualizza turni di oggi/domani
• **Visualizza Squadre:** Elenco completo con componenti
• **Gestisci Componenti:** Assegna VVF alle squadre
"""
    await update.message.reply_text(help_text)

# === GESTIONE BOTTONI INLINE ===
async def button_handler_cambi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce tutti i callback dei bottoni inline"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if user_id != MY_USER_ID:
        await query.message.reply_text("❌ Accesso riservato.")
        return

    # ROUTING CALLBACK
    if data == "squadre_visualizza":
        await mostra_visualizza_squadre(update, context)
        
    elif data == "squadre_aggiungi_componente":
        await mostra_selezione_vvf_per_squadra(update, context)
        
    elif data == "squadre_domani":
        await mostra_chi_tocca_domani(update, context)
        
    elif data == "vvf_visualizza":
        await mostra_tutti_vvf(update, context)
        
    elif data == "vvf_aggiungi":
        await avvia_wizard_aggiungi_vvf(update, context)
        
    elif data.startswith("cambio_data_"):
        await gestisci_selezione_data_cambio(update, context)

async def mostra_visualizza_squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra tutte le squadre organizzate per tipo"""
    conn = get_conn()
    c = conn.cursor()
    
    c.execute('SELECT id, nome, descrizione FROM tipi_squadra ORDER BY id')
    tipi_squadra = c.fetchall()
    
    messaggio = "🏃‍♂️ **ELENCO SQUADRE COMPLETO**\n\n"
    
    for tipo_id, nome_tipo, descrizione in tipi_squadra:
        messaggio += f"**{nome_tipo}** ({descrizione})\n"
        
        c.execute('''
            SELECT s.nome, COUNT(sc.vvf_id)
            FROM squadre s
            LEFT JOIN squadre_componenti sc ON s.id = sc.squadra_id
            WHERE s.tipo_squadra_id = ?
            GROUP BY s.id
            ORDER BY s.ordine
        ''', (tipo_id,))
        
        squadre = c.fetchall()
        for nome_squadra, numero_componenti in squadre:
            messaggio += f"• **{nome_squadra}:** {numero_componenti} componenti\n"
        messaggio += "\n"
    
    conn.close()
    await update.callback_query.edit_message_text(messaggio)

async def mostra_tutti_vvf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra tutti i VVF nel database"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        SELECT qualifica, cognome, nome, autista 
        FROM vvf 
        ORDER BY qualifica, autista, cognome, nome
    ''')
    vvf_lista = c.fetchall()
    conn.close()
    
    if not vvf_lista:
        await update.callback_query.edit_message_text("📝 Nessun VVF presente nel database.")
        return
    
    messaggio = "👥 **ELENCO COMPLETO VVF**\n\n"
    
    csvs = [f"{cognome} {nome}" for qual, cognome, nome, autista in vvf_lista if qual == 'CSV']
    vvf_iii = [f"{cognome} {nome} (III)" for qual, cognome, nome, autista in vvf_lista if qual == 'VV' and autista == 'III']
    vvf_ii = [f"{cognome} {nome} (II)" for qual, cognome, nome, autista in vvf_lista if qual == 'VV' and autista == 'II']
    vvf_i = [f"{cognome} {nome} (I)" for qual, cognome, nome, autista in vvf_lista if qual == 'VV' and autista == 'I']
    
    if csvs:
        messaggio += "**CSV:**\n" + "\n".join(f"• {csv}" for csv in csvs) + "\n\n"
    if vvf_iii:
        messaggio += "**VV Autista III:**\n" + "\n".join(f"• {vvf}" for vvf in vvf_iii) + "\n\n"
    if vvf_ii:
        messaggio += "**VV Autista II:**\n" + "\n".join(f"• {vvf}" for vvf in vvf_ii) + "\n\n"
    if vvf_i:
        messaggio += "**VV Autista I:**\n" + "\n".join(f"• {vvf}" for vvf in vvf_i)
    
    await update.callback_query.edit_message_text(messaggio)

async def avvia_wizard_aggiungi_vvf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avvia il wizard per aggiungere un VVF"""
    context.user_data['wizard_vvf'] = {'step': 'qualifica'}
    
    keyboard = [
        [InlineKeyboardButton("VV", callback_data="vvf_qualifica_VV")],
        [InlineKeyboardButton("CSV", callback_data="vvf_qualifica_CSV")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        "👤 **AGGIUNGI NUOVO VVF**\n\nSeleziona la qualifica:",
        reply_markup=reply_markup
    )

async def mostra_selezione_vvf_per_squadra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra selezione VVF per aggiungere a squadra"""
    await update.callback_query.edit_message_text("🔧 Funzione in sviluppo...")

async def mostra_chi_tocca_domani(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra chi tocca domani"""
    domani = datetime.now() + timedelta(days=1)
    messaggio = f"📅 **CHI TOCCA DOMANI** ({domani.strftime('%d/%m')})\n\n"
    
    # Logica semplificata per domani
    if domani.weekday() == 6:  # Domenica
        squadra_weekend = calcola_squadra_di_turno("Squadre Weekend", domani)
        messaggio += f"🎯 **Weekend:** {squadra_weekend}\n"
    else:
        squadra_sera = calcola_squadra_di_turno("Squadre Sere", domani)
        messaggio += f"🌙 **Sera (20-24):** {squadra_sera}\n"
    
    await update.callback_query.edit_message_text(messaggio)

async def gestisci_selezione_data_cambio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce la selezione della data per il cambio"""
    data_str = update.callback_query.data.replace("cambio_data_", "")
    data = datetime.strptime(data_str, "%Y-%m-%d")
    
    await update.callback_query.edit_message_text(
        f"📅 **Data selezionata:** {data.strftime('%d/%m/%Y')}\n\n"
        "🔧 Wizard cambio in sviluppo..."
    )

# === SISTEMA BACKUP ===
def backup_database_cambi():
    """Backup del database cambi su GitHub Gist"""
    if not GITHUB_TOKEN or not GIST_ID_CAMBI:
        return False
    
    try:
        with open(DATABASE_CAMBI, 'rb') as f:
            db_content = f.read()
        
        db_base64 = base64.b64encode(db_content).decode('utf-8')
        
        files = {
            'cambi_vvf_backup.json': {
                'content': json.dumps({
                    'timestamp': datetime.now().isoformat(),
                    'database_size': len(db_content),
                    'database_base64': db_base64,
                    'backup_type': 'automatic_cambi'
                })
            }
        }
        
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        url = f'https://api.github.com/gists/{GIST_ID_CAMBI}'
        response = requests.patch(url, headers=headers, json={'files': files})
        
        if response.status_code == 200:
            logger.info("✅ Backup cambi completato")
            return True
        return False
        
    except Exception as e:
        logger.error(f"❌ Errore backup cambi: {e}")
        return False

def backup_scheduler_cambi():
    """Scheduler backup per database cambi"""
    while True:
        time.sleep(1800)  # 30 minuti
        backup_database_cambi()

# === WEB SERVER FLASK PER WEBHOOK ===
app_cambi = Flask(__name__)

@app_cambi.route('/')
def home_cambi():
    return "🤖 Bot Gestione Cambi VVF - WEBHOOK 🟢"

@app_cambi.route('/health')
def health_cambi():
    return "OK"

@app_cambi.route('/webhook', methods=['POST'])
def webhook():
    """Endpoint per ricevere gli update da Telegram"""
    if request.method == "POST":
        json_data = request.get_json()
        
        # Inserisci l'update nella coda dell'applicazione
        update = Update.de_json(json_data, application.bot)
        asyncio.run_coroutine_threadsafe(
            application.update_queue.put(update), 
            application._get_running_loop()
        )
        
    return "OK"

def run_flask_cambi():
    app_cambi.run(host='0.0.0.0', port=WEBHOOK_PORT, debug=False, use_reloader=False)

# === MAIN CON WEBHOOK ===
def main_cambi():
    """Funzione principale del bot cambi con WEBHOOK"""
    print("🚀 Avvio Bot Gestione Cambi VVF con WEBHOOK...")
    
    # Configura application globale
    global application
    application = Application.builder().token(BOT_TOKEN_CAMBI).build()
    
    # Aggiungi handler
    application.add_handler(CommandHandler("start", start_cambi))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_cambi))
    application.add_handler(CallbackQueryHandler(button_handler_cambi))
    
    # Avvia Flask in thread separato
    flask_thread = threading.Thread(target=run_flask_cambi, daemon=True)
    flask_thread.start()
    
    # Avvia backup scheduler
    backup_thread = threading.Thread(target=backup_scheduler_cambi, daemon=True)
    backup_thread.start()
    
    # Configura webhook
    print(f"🔄 Configurazione webhook su {WEBHOOK_URL}/webhook")
    
    try:
        # Imposta il webhook su Telegram
        application.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            # secret_token='WEBHOOK_SECRET'  # Opzionale
        )
        
        print("✅ Webhook configurato con successo!")
        print("🤖 Bot Cambi VVF in ascolto su webhook...")
        print("📍 Porta:", WEBHOOK_PORT)
        
        # Avvia l'applicazione con webhook
        application.run_webhook(
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            webhook_url=f"{WEBHOOK_URL}/webhook",
            secret_token='WEBHOOK_SECRET'  # Opzionale
        )
        
    except Exception as e:
        print(f"❌ Errore avvio webhook: {e}")
        # Fallback a polling in caso di errore
        print("🔄 Fallback a polling...")
        application.run_polling()

if __name__ == '__main__':
    main_cambi()
