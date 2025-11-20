import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from datetime import datetime, timedelta
import asyncio
import os
from flask import Flask
import threading
import requests
import time
import psutil
import base64
import json

# === CONFIGURAZIONE ===
DATABASE_NAME = 'autoprotettori_v3.db'
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [1816045269, 653425963, 693843502, 6622015744]

# Configurazione backup GitHub
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GIST_ID = os.environ.get('GIST_ID')

# SOGLIE BOMBOLE
SOGLIE_BOMBOLE = {
    "sotto_scorta": 7,
    "allarme_scorta": 8,  
    "preallarme": 10
}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# === DATABASE ===
def init_db():
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS articoli
                 (id INTEGER PRIMARY KEY,
                  seriale TEXT UNIQUE,
                  categoria TEXT,
                  sede TEXT,
                  stato TEXT DEFAULT 'disponibile',
                  data_inserimento TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute('''CREATE TABLE IF NOT EXISTS utenti
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  nome TEXT,
                  ruolo TEXT DEFAULT 'in_attesa',
                  data_richiesta TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  data_approvazione TIMESTAMP)''')

    for admin_id in ADMIN_IDS:
        c.execute('''INSERT OR IGNORE INTO utenti 
                     (user_id, nome, ruolo, data_approvazione) 
                     VALUES (?, 'Admin', 'admin', CURRENT_TIMESTAMP)''', (admin_id,))

    conn.commit()
    conn.close()

init_db()

# === CATEGORIE E SEDI ===
CATEGORIE = {
    "bombola": "⚗️ Bombola",
    "maschera": "🎭 Maschera",
    "erogatore": "💨 Erogatore", 
    "spallaccio": "🎽 Spallaccio",
    "seconda_utenza": "🏠 Seconda Utenza"
}

SEDI = {
    "erba": "🌿 Erba",
    "centrale": "🏢 Centrale"
}

ORDINE_CATEGORIE = ["bombola", "maschera", "erogatore", "spallaccio", "seconda_utenza"]

# === FUNZIONI UTILITY ===
def is_admin(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("SELECT ruolo FROM utenti WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result and result[0] == 'admin'

def is_user_approved(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("SELECT ruolo FROM utenti WHERE user_id = ? AND ruolo IN ('admin', 'user')", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def get_richieste_in_attesa():
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute('''SELECT user_id, username, nome, data_richiesta 
                 FROM utenti WHERE ruolo = 'in_attesa' ORDER BY data_richiesta''')
    result = c.fetchall()
    conn.close()
    return result

def approva_utente(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute('''UPDATE utenti SET ruolo = 'user', data_approvazione = CURRENT_TIMESTAMP 
                 WHERE user_id = ?''', (user_id,))
    conn.commit()
    conn.close()

# === FUNZIONI GESTIONE CENTRALE ===
def sposta_in_centrale(seriale):
    """Sposta un articolo in centrale mantenendo lo stato originale"""
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    
    c.execute("SELECT stato FROM articoli WHERE seriale = ?", (seriale,))
    risultato = c.fetchone()
    
    if risultato:
        stato_attuale = risultato[0]
        nuovo_stato = ""
        
        if stato_attuale == "usato":
            nuovo_stato = "usato_centrale"
        elif stato_attuale == "fuori_uso":
            nuovo_stato = "fuori_uso_centrale"
        else:
            conn.close()
            return False
        
        c.execute("UPDATE articoli SET stato = ? WHERE seriale = ?", (nuovo_stato, seriale))
        conn.commit()
        conn.close()
        return True
    
    conn.close()
    return False

def get_articoli_in_centrale():
    """Restituisce tutti gli articoli attualmente in centrale"""
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("SELECT seriale, categoria, sede, stato FROM articoli WHERE stato IN ('usato_centrale', 'fuori_uso_centrale')")
    result = c.fetchall()
    conn.close()
    return result

def get_articoli_per_stato_centrale(stato, escludi_centrale=True):
    """Restituisce articoli per stato in centrale, escludendo quelli già in centrale"""
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    
    if escludi_centrale:
        if stato == 'usato':
            c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato = ? AND stato != 'usato_centrale'", (stato,))
        elif stato == 'fuori_uso':
            c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato = ? AND stato != 'fuori_uso_centrale'", (stato,))
        else:
            c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato = ?", (stato,))
    else:
        c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato = ?", (stato,))
        
    result = c.fetchall()
    conn.close()
    return result

# === FUNZIONI ARTICOLI ===
def get_prefisso_categoria(categoria):
    prefissi = {
        "maschera": "MAS",
        "erogatore": "ER", 
        "spallaccio": "SPAL",
        "bombola": "BOMB",
        "seconda_utenza": "2aUT"
    }
    return prefissi.get(categoria, "ART")

def insert_articolo(seriale, categoria, sede, stato="disponibile"):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    try:
        c.execute('''INSERT INTO articoli (seriale, categoria, sede, stato) 
                     VALUES (?, ?, ?, ?)''', (seriale, categoria, sede, stato))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def update_stato(seriale, stato):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("UPDATE articoli SET stato = ? WHERE seriale = ?", (stato, seriale))
    conn.commit()
    conn.close()

def delete_articolo(seriale):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM articoli WHERE seriale = ?", (seriale,))
    conn.commit()
    conn.close()

def get_articoli_per_stato(stato):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    
    if stato == 'usato':
        c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato IN ('usato', 'usato_centrale')")
    elif stato == 'fuori_uso':
        c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato IN ('fuori_uso', 'fuori_uso_centrale')")
    elif stato == 'disponibile':
        c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato = ?", (stato,))
    else:
        c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato = ?", (stato,))
        
    result = c.fetchall()
    conn.close()
    return result

def get_tutti_articoli():
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("SELECT seriale, categoria, sede, stato FROM articoli")
    result = c.fetchall()
    conn.close()
    return result

def conta_bombole_disponibili():
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute('''SELECT COUNT(*) FROM articoli 
                 WHERE categoria = 'bombola' AND stato = 'disponibile' ''')
    risultato = c.fetchone()[0]
    conn.close()
    return risultato

def get_categorie_con_articoli(stato=None):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    
    if stato:
        c.execute('''SELECT DISTINCT categoria FROM articoli WHERE stato = ?''', (stato,))
    else:
        c.execute('''SELECT DISTINCT categoria FROM articoli''')
    
    result = [row[0] for row in c.fetchall()]
    conn.close()
    return result

def organizza_articoli_per_categoria(articoli):
    articoli_organizzati = {}
    
    for categoria in ORDINE_CATEGORIE:
        articoli_organizzati[categoria] = []
    
    for articolo in articoli:
        if len(articolo) == 4:
            seriale, cat, sede, stato = articolo
        else:
            seriale, cat, sede = articolo
            stato = None
            
        if cat in articoli_organizzati:
            articoli_organizzati[cat].append((seriale, sede, stato))
    
    return articoli_organizzati

# === FUNZIONE HELP ===
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    help_text = """
🤖 **BENVENUTO IN AUTOPROTETTORI ERBA!**

🎯 **COSA PUOI FARE:**

👤 **COME UTENTE:**
• 📋 Vedere l'inventario completo
• 🔴 Segnare articoli usati dopo l'utilizzo
• 🟢 Controllare disponibilità in tempo reale
• 📊 Monitorare stati (disponibili/usati/fuori uso)
• 📍 Gestire articoli in centrale

👨‍💻 **COME ADMIN:**
• ➕ Aggiungere nuovi articoli all'inventario
• ➖ Rimuovere articoli tramite interfaccia semplice
• 🔄 Ripristinare articoli usati o fuori uso
• 📈 Visualizzare statistiche dettagliate
• ⚠️ Ricevere allarmi automatici per scorte bombole
• 👥 Gestire richieste accesso nuovi utenti

🔄 **SISTEMA SEMPRE ATTIVO:**
• ✅ Servizio 24/7 garantito
"""

    await update.message.reply_text(help_text, reply_markup=crea_tastiera_fisica(user_id))

# === TASTIERA FISICA ===
def crea_tastiera_fisica(user_id):
    if not is_user_approved(user_id):
        return ReplyKeyboardMarkup([[KeyboardButton("🚀 Richiedi Accesso")]], resize_keyboard=True)

    tastiera = [
        [KeyboardButton("📋 Inventario"), KeyboardButton("🔴 Segna Usato")],
        [KeyboardButton("🟢 Disponibili"), KeyboardButton("🔴 Usati")],
        [KeyboardButton("⚫ Fuori Uso"), KeyboardButton("📍 In Centrale")],
        [KeyboardButton("🆘 Help")]
    ]

    if is_admin(user_id):
        tastiera.append([KeyboardButton("➕ Aggiungi"), KeyboardButton("➖ Rimuovi")])
        tastiera.append([KeyboardButton("🔄 Ripristina"), KeyboardButton("📊 Statistiche")])
        tastiera.append([KeyboardButton("👥 Gestisci Richieste")])

    return ReplyKeyboardMarkup(tastiera, resize_keyboard=True, is_persistent=True)

# === HANDLER START ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO utenti (user_id, username, nome, ruolo) 
                 VALUES (?, ?, ?, 'in_attesa')''', 
                 (user_id, update.effective_user.username, user_name))
    conn.commit()
    conn.close()

    if not is_user_approved(user_id):
        richieste = get_richieste_in_attesa()
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🆕 NUOVA RICHIESTA ACCESSO\n\nUser: {user_name}\nID: {user_id}\nRichieste in attesa: {len(richieste)}"
                )
            except:
                pass

        await update.message.reply_text(
            "✅ Richiesta inviata agli amministratori.\nAttendi l'approvazione!",
            reply_markup=crea_tastiera_fisica(user_id)
        )
        return

    if is_admin(user_id):
        welcome_text = f"👨‍💻 BENVENUTO ADMIN {user_name}!"
    else:
        welcome_text = f"👤 BENVENUTO {user_name}!"

    await update.message.reply_text(welcome_text, reply_markup=crea_tastiera_fisica(user_id))

# === GESTIONE RICHIESTE ACCESSO ===
async def gestisci_richieste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    richieste = get_richieste_in_attesa()
    if not richieste:
        await update.message.reply_text("✅ Nessuna richiesta di accesso in sospeso.")
        return

    prima_richiesta = richieste[0]
    user_id_rich, username, nome, data_richiesta = prima_richiesta
    data = data_richiesta.split()[0] if data_richiesta else "N/A"
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Approva", callback_data=f"approva_{user_id_rich}"),
            InlineKeyboardButton("❌ Rifiuta", callback_data=f"rifiuta_{user_id_rich}")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    richieste_rimanenti = len(richieste) - 1
    info_rimanenti = f"\n\n📋 Richieste rimanenti in attesa: {richieste_rimanenti}" if richieste_rimanenti > 0 else ""
    
    await update.message.reply_text(
        f"👤 **RICHIESTA ACCESSO DA APPROVARE**\n\n"
        f"🆔 **ID:** {user_id_rich}\n"
        f"👤 **Nome:** {nome}\n"
        f"📱 **Username:** @{username}\n"
        f"📅 **Data richiesta:** {data}\n\n"
        f"Seleziona un'azione:{info_rimanenti}",
        reply_markup=reply_markup
    )

# === HANDLER MESSAGGI PRINCIPALE ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not is_user_approved(user_id):
        if text == "🚀 Richiedi Accesso":
            await start(update, context)
        return

    # INVENTARIO
    elif text == "📋 Inventario":
        articoli = get_tutti_articoli()
        if not articoli:
            await update.message.reply_text("📦 Inventario vuoto")
            return

        msg = "📋 **INVENTARIO COMPLETO**\n\n"
        
        disponibili = [a for a in articoli if a[3] == 'disponibile']
        usati = [a for a in articoli if a[3] in ['usato', 'usato_centrale']]
        fuori_uso = [a for a in articoli if a[3] in ['fuori_uso', 'fuori_uso_centrale']]
        
        if disponibili:
            msg += f"🟢 **DISPONIBILI** ({len(disponibili)}):\n"
            disponibili_organizzati = organizza_articoli_per_categoria(disponibili)
            
            for categoria in ORDINE_CATEGORIE:
                articoli_cat = disponibili_organizzati[categoria]
                if articoli_cat:
                    msg += f"\n**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                    articoli_cat.sort(key=lambda x: x[0], reverse=True)
                    for seriale, sede, _ in articoli_cat:
                        msg += f"• {seriale} - {SEDI[sede]}\n"
            msg += "\n"
        
        if usati:
            msg += f"🔴 **USATI** ({len(usati)}):\n"
            usati_organizzati = organizza_articoli_per_categoria(usati)
            
            for categoria in ORDINE_CATEGORIE:
                articoli_cat = usati_organizzati[categoria]
                if articoli_cat:
                    msg += f"\n**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                    articoli_cat.sort(key=lambda x: x[0], reverse=True)
                    for seriale, sede, stato in articoli_cat:
                        locazione = " (Centrale)" if stato == 'usato_centrale' else ""
                        msg += f"• {seriale} - {SEDI[sede]}{locazione}\n"
            msg += "\n"
        
        if fuori_uso:
            msg += f"⚫ **FUORI USO** ({len(fuori_uso)}):\n"
            fuori_uso_organizzati = organizza_articoli_per_categoria(fuori_uso)
            
            for categoria in ORDINE_CATEGORIE:
                articoli_cat = fuori_uso_organizzati[categoria]
                if articoli_cat:
                    msg += f"\n**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                    articoli_cat.sort(key=lambda x: x[0], reverse=True)
                    for seriale, sede, stato in articoli_cat:
                        locazione = " (Centrale)" if stato == 'fuori_uso_centrale' else ""
                        msg += f"• {seriale} - {SEDI[sede]}{locazione}\n"
        
        msg += f"\n📊 **Totale articoli:** {len(articoli)}"
        await update.message.reply_text(msg)

    # SEGNA USATO
    elif text == "🔴 Segna Usato":
        categorie_con_articoli = get_categorie_con_articoli('disponibile')
        
        if not categorie_con_articoli:
            await update.message.reply_text("✅ Nessun articolo da segnare come usato")
            return

        keyboard = []
        for categoria in categorie_con_articoli:
            if categoria in CATEGORIE:
                keyboard.append([InlineKeyboardButton(CATEGORIE[categoria], callback_data=f"usato_cat_{categoria}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("🔴 Seleziona categoria per segnare como USATO:", reply_markup=reply_markup)

    # DISPONIBILI
    elif text == "🟢 Disponibili":
        articoli = get_articoli_per_stato('disponibile')
        if not articoli:
            await update.message.reply_text("🟢 Nessun articolo disponibile")
            return
        
        msg = f"🟢 **ARTICOLI DISPONIBILI** ({len(articoli)})\n\n"
        articoli_organizzati = organizza_articoli_per_categoria([(a[0], a[1], a[2], 'disponibile') for a in articoli])
        
        for categoria in ORDINE_CATEGORIE:
            articoli_cat = articoli_organizzati[categoria]
            if articoli_cat:
                msg += f"**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                articoli_cat.sort(key=lambda x: x[0], reverse=True)
                for seriale, sede, _ in articoli_cat:
                    msg += f"• {seriale} - {SEDI[sede]}\n"
                msg += "\n"
        
        await update.message.reply_text(msg)

    # USATI
    elif text == "🔴 Usati":
        articoli = get_articoli_per_stato('usato')
        if not articoli:
            await update.message.reply_text("🔴 Nessun articolo usato")
            return
        
        msg = f"🔴 **ARTICOLI USATI** ({len(articoli)})\n\n"
        articoli_organizzati = organizza_articoli_per_categoria([(a[0], a[1], a[2], 'usato') for a in articoli])
        
        for categoria in ORDINE_CATEGORIE:
            articoli_cat = articoli_organizzati[categoria]
            if articoli_cat:
                msg += f"**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                articoli_cat.sort(key=lambda x: x[0], reverse=True)
                for seriale, sede, _ in articoli_cat:
                    locazione = " (Centrale)" if any(a[0] == seriale and a[3] == 'usato_centrale' for a in get_tutti_articoli()) else ""
                    msg += f"• {seriale} - {SEDI[sede]}{locazione}\n"
                msg += "\n"
        
        await update.message.reply_text(msg)

    # FUORI USO
    elif text == "⚫ Fuori Uso":
        if not is_admin(user_id):
            articoli_fuori_uso = get_articoli_per_stato('fuori_uso')
            if not articoli_fuori_uso:
                await update.message.reply_text("⚫ Nessun articolo fuori uso")
                return
            
            msg = f"⚫ **ARTICOLI FUORI USO** ({len(articoli_fuori_uso)})\n\n"
            articoli_organizzati = organizza_articoli_per_categoria([(a[0], a[1], a[2], 'fuori_uso') for a in articoli_fuori_uso])
            
            for categoria in ORDINE_CATEGORIE:
                articoli_cat = articoli_organizzati[categoria]
                if articoli_cat:
                    msg += f"**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                    articoli_cat.sort(key=lambda x: x[0], reverse=True)
                    for seriale, sede, _ in articoli_cat:
                        locazione = " (Centrale)" if any(a[0] == seriale and a[3] == 'fuori_uso_centrale' for a in get_tutti_articoli()) else ""
                        msg += f"• {seriale} - {SEDI[sede]}{locazione}\n"
                    msg += "\n"
            
            msg += "ℹ️ Solo gli amministratori possono modificare lo stato."
            await update.message.reply_text(msg)
            return

        categorie_con_articoli = get_categorie_con_articoli('disponibile') + get_categorie_con_articoli('usato')
        categorie_con_articoli = list(set(categorie_con_articoli))
        
        if not categorie_con_articoli:
            await update.message.reply_text("⚫ Nessun articolo da segnare como fuori uso")
            return

        keyboard = []
        for categoria in categorie_con_articoli:
            if categoria in CATEGORIE:
                keyboard.append([InlineKeyboardButton(CATEGORIE[categoria], callback_data=f"crea_fuori_uso_cat_{categoria}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("⚫ Seleziona categoria per SEGNARE como FUORI USO:", reply_markup=reply_markup)

    # AGGIUNGI (solo admin)
    elif text == "➕ Aggiungi" and is_admin(user_id):
        context.user_data['azione'] = 'aggiungi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"nuovo_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("📦 Seleziona categoria:", reply_markup=reply_markup)

    # RIMUOVI (solo admin)
    elif text == "➖ Rimuovi" and is_admin(user_id):
        context.user_data['azione'] = 'rimuovi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"rimuovi_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("➖ Seleziona categoria:", reply_markup=reply_markup)

    # RIPRISTINA (solo admin)
    elif text == "🔄 Ripristina" and is_admin(user_id):
        articoli_usati = get_articoli_per_stato('usato')
        articoli_fuori_uso = get_articoli_per_stato('fuori_uso')
        articoli = articoli_usati + articoli_fuori_uso

        if not articoli:
            await update.message.reply_text("✅ Nessun articolo da ripristinare")
            return

        context.user_data['selezioni_ripristina'] = []
        
        keyboard = []
        articoli.sort(key=lambda x: x[0], reverse=True)
        for seriale, cat, sed in articoli:
            stato_attuale = "usato" if (seriale, cat, sed) in articoli_usati else "fuori uso"
            nome = f"{seriale} - {CATEGORIE[cat]} ({stato_attuale})"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"seleziona_ripristina_{seriale}")])
        
        keyboard.append([InlineKeyboardButton("✅ CONFERMA SELEZIONE", callback_data="conferma_ripristina")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🔄 Seleziona articoli da ripristinare a DISPONIBILE:\n\n"
            "🟢 Clicca sugli articoli che vuoi selezionare, poi clicca CONFERMA SELEZIONE\n"
            "📝 Articoli selezionati: 0",
            reply_markup=reply_markup
        )

    # STATISTICHE (solo admin)
    elif text == "📊 Statistiche" and is_admin(user_id):
        articoli = get_tutti_articoli()
        totale = len(articoli)
        disponibili = len([a for a in articoli if a[3] == 'disponibile'])
        usati = len([a for a in articoli if a[3] in ['usato', 'usato_centrale']])
        fuori_uso = len([a for a in articoli if a[3] in ['fuori_uso', 'fuori_uso_centrale']])

        bombole_totali = conta_bombole_disponibili()

        msg = "📊 **STATISTICHE COMPLETE**\n\n"
        msg += f"📦 **Totale articoli:** {totale}\n"
        msg += f"🟢 **Disponibili:** {disponibili}\n"
        msg += f"🔴 **Usati:** {usati}\n"
        msg += f"⚫ **Fuori uso:** {fuori_uso}\n\n"

        msg += "⚗️ **BOMBOLE DISPONIBILI (TOTALE):**\n"
        msg += f"🌿🏢 **Combinate (Erba + Centrale):** {bombole_totali}"
        if bombole_totali <= SOGLIE_BOMBOLE["sotto_scorta"]:
            msg += " 🚨 **SOTTO SCORTA!**"
        elif bombole_totali <= SOGLIE_BOMBOLE["allarme_scorta"]:
            msg += " 🟡 **ALLARME SCORTA!**"
        elif bombole_totali <= SOGLIE_BOMBOLE["preallarme"]:
            msg += " 🔶 **PREALLARME!**"
        else:
            msg += " ✅ **Ok**"

        await update.message.reply_text(msg)

    # GESTIONE RICHIESTE (solo admin)
    elif text == "👥 Gestisci Richieste" and is_admin(user_id):
        await gestisci_richieste(update, context)

    # HELP
    elif text == "🆘 Help":
        await help_command(update, context)

    # IN CENTRALE - CORRETTO
    elif text == "📍 In Centrale":
        if not is_user_approved(user_id):
            await update.message.reply_text("❌ Accesso non autorizzato")
            return

        keyboard = [
            [InlineKeyboardButton("📤 Sposta Usati in Centrale", callback_data="centrale_sposta_usati")],
            [InlineKeyboardButton("📤 Sposta Fuori Uso in Centrale", callback_data="centrale_sposta_fuori_uso")],
            [InlineKeyboardButton("📋 Inventario Centrale", callback_data="centrale_inventario")],
        ]
        
        articoli_centrale = get_articoli_in_centrale()
        usati_centrale = len([a for a in articoli_centrale if a[3] == 'usato_centrale'])
        fuori_uso_centrale = len([a for a in articoli_centrale if a[3] == 'fuori_uso_centrale'])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        messaggio = f"🏢 **GESTIONE ARTICOLI IN CENTRALE**\n\n"
        messaggio += f"📊 **Attualmente in centrale:**\n"
        messaggio += f"• 🔴 Usati: {usati_centrale}\n"
        messaggio += f"• ⚫ Fuori uso: {fuori_uso_centrale}\n"
        messaggio += f"• 📦 Totale: {len(articoli_centrale)}\n\n"
        messaggio += "Seleziona un'operazione:"
        
        await update.message.reply_text(messaggio, reply_markup=reply_markup)

    # INSERIMENTO NUMERO
    elif context.user_data.get('azione') == 'inserisci_numero':
        numero = text.strip()
        categoria = context.user_data['categoria_da_aggiungere']
        sede = context.user_data['sede_da_aggiungere']
        
        if not numero.isdigit() or len(numero) != 3:
            await update.message.reply_text(
                "❌ Formato numero non valido!\n"
                "Inserisci esattamente 3 cifre (es. 001, 123, 999)\n\n"
                "Riprova:"
            )
            return
        
        prefisso = get_prefisso_categoria(categoria)
        seriale = f"{prefisso}_{numero}_{sede.upper()}"
        
        if insert_articolo(seriale, categoria, sede):
            await update.message.reply_text(
                f"✅ ARTICOLO AGGIUNTO!\n\nSeriale: {seriale}\nCategoria: {CATEGORIE[categoria]}\nSede: {SEDI[sede]}"
            )
            
            if categoria == 'bombola':
                await controlla_allarme_bombole(context)
        else:
            await update.message.reply_text(f"❌ {seriale} già esistente!")
        
        for key in ['azione', 'categoria_da_aggiungere', 'sede_da_aggiungere']:
            if key in context.user_data:
                del context.user_data[key]

    else:
        await update.message.reply_text("ℹ️ Usa i pulsanti per navigare.", reply_markup=crea_tastiera_fisica(user_id))

# === GESTIONE BOTTONI INLINE - SEZIONE CENTRALE CORRETTA ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # GESTIONE CENTRALE - SPOSTA USATI
    if data == "centrale_sposta_usati":
        articoli_usati = get_articoli_per_stato_centrale('usato', escludi_centrale=True)
        if not articoli_usati:
            await query.edit_message_text("❌ Nessun articolo usato da spostare in centrale (o tutti già in centrale)")
            return

        context.user_data['selezioni_centrale_usati'] = []
        
        keyboard = []
        articoli_usati.sort(key=lambda x: x[0], reverse=True)
        for seriale, cat, sed in articoli_usati:
            nome = f"{seriale} - {SEDI[sed]}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"seleziona_centrale_usato_{seriale}")])
        
        keyboard.append([InlineKeyboardButton("✅ CONFERMA SELEZIONE", callback_data="conferma_centrale_usati")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📤 Seleziona articoli USATI da spostare in CENTRALE:\n\n"
            "🟢 Clicca sugli articoli che vuoi selezionare, poi clicca CONFERMA SELEZIONE\n"
            "📝 Articoli selezionati: 0",
            reply_markup=reply_markup
        )

    # SELEZIONE ARTICOLO USATO PER CENTRALE - CORRETTO
    elif data.startswith("seleziona_centrale_usato_"):
        seriale = data[24:]
        selezioni = context.user_data.get('selezioni_centrale_usati', [])
        
        if seriale in selezioni:
            selezioni.remove(seriale)
        else:
            selezioni.append(seriale)
        
        context.user_data['selezioni_centrale_usati'] = selezioni
        
        # RICREA COMPLETAMENTE LA TASTIERA AGGIORNATA
        articoli_usati = get_articoli_per_stato_centrale('usato', escludi_centrale=True)
        
        keyboard = []
        articoli_usati.sort(key=lambda x: x[0], reverse=True)
        for art_seriale, cat, sed in articoli_usati:
            nome = f"{art_seriale} - {SEDI[sed]}"
            # AGGIUNGI LA SPUNTA SE SELEZIONATO
            if art_seriale in selezioni:
                nome = f"✅ {nome}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"seleziona_centrale_usato_{art_seriale}")])
        
        keyboard.append([InlineKeyboardButton("✅ CONFERMA SELEZIONE", callback_data="conferma_centrale_usati")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📤 Seleziona articoli USATI da spostare in CENTRALE:\n\n"
            f"🟢 Clicca sugli articoli che vuoi selezionare, poi clicca CONFERMA SELEZIONE\n"
            f"📝 Articoli selezionati: {len(selezioni)}",
            reply_markup=reply_markup
        )

    # CONFERMA SPOSTAMENTO USATI IN CENTRALE - CORRETTO
    elif data == "conferma_centrale_usati":
        selezioni = context.user_data.get('selezioni_centrale_usati', [])
        
        if not selezioni:
            await query.answer("❌ Nessun articolo selezionato!", show_alert=True)
            return
        
        success_count = 0
        for seriale in selezioni:
            if sposta_in_centrale(seriale):
                success_count += 1
        
        await query.edit_message_text(f"✅ {success_count} articoli usati spostati in CENTRALE!")
        
        if 'selezioni_centrale_usati' in context.user_data:
            del context.user_data['selezioni_centrale_usati']

    # GESTIONE CENTRALE - SPOSTA FUORI USO
    elif data == "centrale_sposta_fuori_uso":
        articoli_fuori_uso = get_articoli_per_stato_centrale('fuori_uso', escludi_centrale=True)
        if not articoli_fuori_uso:
            await query.edit_message_text("❌ Nessun articolo fuori uso da spostare in centrale (o tutti già in centrale)")
            return

        context.user_data['selezioni_centrale_fuori_uso'] = []
        
        keyboard = []
        articoli_fuori_uso.sort(key=lambda x: x[0], reverse=True)
        for seriale, cat, sed in articoli_fuori_uso:
            nome = f"{seriale} - {SEDI[sed]}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"seleziona_centrale_fuori_uso_{seriale}")])
        
        keyboard.append([InlineKeyboardButton("✅ CONFERMA SELEZIONE", callback_data="conferma_centrale_fuori_uso")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📤 Seleziona articoli FUORI USO da spostare in CENTRALE:\n\n"
            "🟢 Clicca sugli articoli che vuoi selezionare, poi clicca CONFERMA SELEZIONE\n"
            "📝 Articoli selezionati: 0",
            reply_markup=reply_markup
        )

    # SELEZIONE ARTICOLO FUORI USO PER CENTRALE - CORRETTO
    elif data.startswith("seleziona_centrale_fuori_uso_"):
        seriale = data[28:]
        selezioni = context.user_data.get('selezioni_centrale_fuori_uso', [])
        
        if seriale in selezioni:
            selezioni.remove(seriale)
        else:
            selezioni.append(seriale)
        
        context.user_data['selezioni_centrale_fuori_uso'] = selezioni
        
        # RICREA COMPLETAMENTE LA TASTIERA AGGIORNATA
        articoli_fuori_uso = get_articoli_per_stato_centrale('fuori_uso', escludi_centrale=True)
        
        keyboard = []
        articoli_fuori_uso.sort(key=lambda x: x[0], reverse=True)
        for art_seriale, cat, sed in articoli_fuori_uso:
            nome = f"{art_seriale} - {SEDI[sed]}"
            # AGGIUNGI LA SPUNTA SE SELEZIONATO
            if art_seriale in selezioni:
                nome = f"✅ {nome}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"seleziona_centrale_fuori_uso_{art_seriale}")])
        
        keyboard.append([InlineKeyboardButton("✅ CONFERMA SELEZIONE", callback_data="conferma_centrale_fuori_uso")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📤 Seleziona articoli FUORI USO da spostare in CENTRALE:\n\n"
            f"🟢 Clicca sugli articoli che vuoi selezionare, poi clicca CONFERMA SELEZIONE\n"
            f"📝 Articoli selezionati: {len(selezioni)}",
            reply_markup=reply_markup
        )

    # CONFERMA SPOSTAMENTO FUORI USO IN CENTRALE - CORRETTO
    elif data == "conferma_centrale_fuori_uso":
        selezioni = context.user_data.get('selezioni_centrale_fuori_uso', [])
        
        if not selezioni:
            await query.answer("❌ Nessun articolo selezionato!", show_alert=True)
            return
        
        success_count = 0
        for seriale in selezioni:
            if sposta_in_centrale(seriale):
                success_count += 1
        
        await query.edit_message_text(f"✅ {success_count} articoli fuori uso spostati in CENTRALE!")
        
        if 'selezioni_centrale_fuori_uso' in context.user_data:
            del context.user_data['selezioni_centrale_fuori_uso']

    # GESTIONE CENTRALE - INVENTARIO
    elif data == "centrale_inventario":
        articoli_centrale = get_articoli_in_centrale()
        if not articoli_centrale:
            await query.edit_message_text("🏢 **INVENTARIO CENTRALE**\n\n📦 Nessun articolo in centrale al momento")
            return

        articoli_organizzati = organizza_articoli_per_categoria([(a[0], a[1], a[2], a[3]) for a in articoli_centrale])
        
        msg = "🏢 **INVENTARIO CENTRALE**\n\n"
        
        usati_centrale = [a for a in articoli_centrale if a[3] == 'usato_centrale']
        if usati_centrale:
            msg += f"🔴 **USATI IN CENTRALE** ({len(usati_centrale)}):\n"
            usati_organizzati = organizza_articoli_per_categoria([(a[0], a[1], a[2], a[3]) for a in usati_centrale])
            
            for categoria in ORDINE_CATEGORIE:
                articoli_cat = usati_organizzati[categoria]
                if articoli_cat:
                    msg += f"\n**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                    articoli_cat.sort(key=lambda x: x[0], reverse=True)
                    for seriale, sede, _ in articoli_cat:
                        msg += f"• {seriale}\n"
            msg += "\n"
        
        fuori_uso_centrale = [a for a in articoli_centrale if a[3] == 'fuori_uso_centrale']
        if fuori_uso_centrale:
            msg += f"⚫ **FUORI USO IN CENTRALE** ({len(fuori_uso_centrale)}):\n"
            fuori_uso_organizzati = organizza_articoli_per_categoria([(a[0], a[1], a[2], a[3]) for a in fuori_uso_centrale])
            
            for categoria in ORDINE_CATEGORIE:
                articoli_cat = fuori_uso_organizzati[categoria]
                if articoli_cat:
                    msg += f"\n**{CATEGORIE[categoria]}** ({len(articoli_cat)}):\n"
                    articoli_cat.sort(key=lambda x: x[0], reverse=True)
                    for seriale, sede, _ in articoli_cat:
                        msg += f"• {seriale}\n"
        
        msg += f"\n📊 **RIASSUNTO CENTRALE:**\n"
        msg += f"• 🔴 Usati: {len(usati_centrale)}\n"
        msg += f"• ⚫ Fuori uso: {len(fuori_uso_centrale)}\n"
        msg += f"• 📦 Totale: {len(articoli_centrale)}"
        
        await query.edit_message_text(msg)

    # APPROVA UTENTE
    elif data.startswith("approva_"):
        if not is_admin(user_id):
            return
            
        user_id_approvare = int(data[8:])
        approva_utente(user_id_approvare)
        
        try:
            await context.bot.send_message(
                user_id_approvare,
                "✅ ACCESSO APPROVATO! Ora puoi usare tutte le funzioni del bot.\nUsa /start per iniziare."
            )
        except:
            pass
            
        richieste_rimanenti = get_richieste_in_attesa()
        if richieste_rimanenti:
            messaggio_aggiuntivo = f"\n\n📋 Ci sono ancora {len(richieste_rimanenti)} richieste in attesa.\nUsa nuovamente '👥 Gestisci Richieste' per continuare."
        else:
            messaggio_aggiuntivo = "\n\n✅ Tutte le richieste sono state gestite."
            
        await query.edit_message_text(f"✅ Utente {user_id_approvare} approvato!{messaggio_aggiuntivo}")

    # RIFIUTA UTENTE
    elif data.startswith("rifiuta_"):
        if not is_admin(user_id):
            return
            
        user_id_rifiutare = int(data[8:])
        conn = sqlite3.connect(DATABASE_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM utenti WHERE user_id = ?", (user_id_rifiutare,))
        conn.commit()
        conn.close()
        
        richieste_rimanenti = get_richieste_in_attesa()
        if richieste_rimanenti:
            messaggio_aggiuntivo = f"\n\n📋 Ci sono ancora {len(richieste_rimanenti)} richieste in attesa.\nUsa nuovamente '👥 Gestisci Richieste' para continuare."
        else:
            messaggio_aggiuntivo = "\n\n✅ Tutte le richieste sono stata gestite."
            
        await query.edit_message_text(f"❌ Utente {user_id_rifiutare} rifiutato!{messaggio_aggiuntivo}")

    # SELEZIONE CATEGORIA PER AGGIUNTA
    elif data.startswith("nuovo_cat_"):
        categoria = data[10:]
        context.user_data['nuova_categoria'] = categoria
        context.user_data['azione'] = 'aggiungi_sede'
        
        keyboard = [
            [InlineKeyboardButton(SEDI[sede], callback_data=f"nuovo_sede_{sede}")] 
            for sede in SEDI
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"🏢 Seleziona sede per {CATEGORIE[categoria]}:", reply_markup=reply_markup)

    # SELEZIONE SEDE PER AGGIUNTA
    elif data.startswith("nuovo_sede_"):
        sede = data[11:]
        categoria = context.user_data['nuova_categoria']
        
        context.user_data['azione'] = 'inserisci_numero'
        context.user_data['categoria_da_aggiungere'] = categoria
        context.user_data['sede_da_aggiungere'] = sede
        
        prefisso = get_prefisso_categoria(categoria)
        await query.edit_message_text(
            f"📝 Inserisci NUMERO per {CATEGORIE[categoria]} - {SEDI[sede]}:\n\n"
            f"Prefisso: {prefisso}\n"
            f"📌 Formato richiesto: **3 cifre** (es. 001, 123, 999)\n\n"
            f"Inserisci le 3 cifre:"
        )

    # ... (altri handler per le altre sezioni rimangono invariati) ...

# === ALLARME BOMBOLE ===
async def controlla_allarme_bombole(context: ContextTypes.DEFAULT_TYPE):
    bombole_totali = conta_bombole_disponibili()

    messaggio = None
    if bombole_totali <= SOGLIE_BOMBOLE["sotto_scorta"]:
        messaggio = f"🚨 SOTTO SCORTA BOMBOLE! Solo {bombole_totali} disponibili in totale (Erba + Centrale)!"
    elif bombole_totali <= SOGLIE_BOMBOLE["allarme_scorta"]:
        messaggio = f"🟡 ALLARME SCORTA BOMBOLE! Solo {bombole_totali} disponibili in totale!"
    elif bombole_totali <= SOGLIE_BOMBOLE["preallarme"]:
        messaggio = f"🔶 PREALLARME SCORTA BOMBOLE! Solo {bombole_totali} disponibili in totale!"

    if messaggio:
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id, messaggio)
            except:
                pass

# === SERVER FLASK ===
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bot Telegram Autoprotettori - ONLINE 🟢"

@app.route('/health')
def health():
    return "OK"

@app.route('/ping')
def ping():
    return f"PONG - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

def run_flask():
    app.run(host='0.0.0.0', port=10000, debug=False)

# === MAIN ===
def main():
    print("🚀 Avvio Bot Autoprotettori Erba...")
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("✅ Flask server started on port 10000")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot Autoprotettori Erba Avviato!")
    print("📍 Server: Render.com")
    print("🟢 Status: ONLINE")
    
    application.run_polling()

if __name__ == '__main__':
    main()
