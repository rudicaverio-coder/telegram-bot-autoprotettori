"""
BOT GESTIONE CAMBI VVF - SISTEMA COMPLETO
Funzionalità: Gestione cambi + Squadre + Chi Tocca
Architettura: Parallelo al bot autoprotettori
"""

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
from typing import Dict, List, Tuple

# === CONFIGURAZIONE ===
BOT_TOKEN_CAMBI = os.environ.get('BOT_TOKEN_CAMBI')
DATABASE_CAMBI = 'cambi_vvf.db'
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GIST_ID_CAMBI = os.environ.get('GIST_ID_CAMBI')

# ID unico utilizzatore
MY_USER_ID = 1816045269

# Configurazione logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

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
    
    # NUOVE TABELLE PER SISTEMA SQUADRE
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
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS turni_squadra (
            id INTEGER PRIMARY KEY,
            tipo_squadra_id INTEGER,
            squadra_id INTEGER,
            data_inizio DATE,
            data_fine DATE,
            note TEXT,
            FOREIGN KEY (tipo_squadra_id) REFERENCES tipi_squadra(id),
            FOREIGN KEY (squadra_id) REFERENCES squadre(id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS festivita (
            id INTEGER PRIMARY KEY,
            data DATE UNIQUE,
            nome TEXT,
            ricorrente BOOLEAN DEFAULT 1
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
        # Weekend ABCD
        (1, 'A', 1), (1, 'B', 2), (1, 'C', 3), (1, 'D', 4),
        # Notti feriali An Bn Cn
        (2, 'An', 1), (2, 'Bn', 2), (2, 'Cn', 3),
        # Notti venerdì S1n S2n
        (3, 'S1n', 1), (3, 'S2n', 2),
        # Sere S1-S7
        (4, 'S1', 1), (4, 'S2', 2), (4, 'S3', 3), 
        (4, 'S4', 4), (4, 'S5', 5), (4, 'S6', 6), (4, 'S7', 7)
    ]
    
    c.executemany('''
        INSERT OR IGNORE INTO squadre (tipo_squadra_id, nome, ordine)
        VALUES (?, ?, ?)
    ''', squadre_predefinite)
    
    # Inserimento festività italiane 2025
    festivita_2025 = [
        ('2025-01-01', 'Capodanno', 1),
        ('2025-01-06', 'Epifania', 1),
        ('2025-04-21', 'Pasqua', 0),
        ('2025-04-25', 'Liberazione', 1),
        ('2025-05-01', 'Festa Lavoro', 1),
        ('2025-06-02', 'Festa Repubblica', 1),
        ('2025-08-15', 'Ferragosto', 1),
        ('2025-11-01', 'Ognissanti', 1),
        ('2025-12-08', 'Immacolata', 1),
        ('2025-12-25', 'Natale', 1),
        ('2025-12-26', 'Santo Stefano', 1)
    ]
    
    c.executemany('''
        INSERT OR IGNORE INTO festivita (data, nome, ricorrente)
        VALUES (?, ?, ?)
    ''', festivita_2025)
    
    conn.commit()
    conn.close()

init_db_cambi()

# === FUNZIONI UTILITY DATABASE ===
def get_conn():
    return sqlite3.connect(DATABASE_CAMBI)

# === SISTEMA "CHI TOCCA" - CALENDARIO INTELLIGENTE ===
def calcola_squadra_di_turno(tipo_squadra: str, data: datetime) -> str:
    """
    CALCOLO INTELLIGENTE: Determina quale squadra è di turno in base a data e tipo
    Logica complessa per rotazione squadre
    """
    conn = get_conn()
    c = conn.cursor()
    
    # Ottieni configurazione tipo squadra
    c.execute('SELECT id, numero_squadre FROM tipi_squadra WHERE nome = ?', (tipo_squadra,))
    tipo = c.fetchone()
    if not tipo:
        conn.close()
        return "N/D"
    
    tipo_id, numero_squadre = tipo
    
    # Ottieni squadre ordinate
    c.execute('''
        SELECT id, nome FROM squadre 
        WHERE tipo_squadra_id = ? 
        ORDER BY ordine
    ''', (tipo_id,))
    squadre = c.fetchall()
    
    # LOGICA DI ROTAZIONE PER OGNI TIPO SQUADRA
    if tipo_squadra == "Squadre Weekend":
        # Weekend: rotazione ABCD ogni settimana
        inizio_anno = datetime(data.year, 1, 1)
        giorni_dall_inizio = (data - inizio_anno).days
        settimana = giorni_dall_inizio // 7
        indice = settimana % numero_squadre
        squadra = squadre[indice][1]
        
    elif tipo_squadra == "Squadre Notti Feriali":
        # Notti feriali: rotazione An Bn Cn giornaliera
        inizio_settimana = data - timedelta(days=data.weekday())
        giorni_dalla_domenica = (data - inizio_settimana).days
        indice = giorni_dalla_domenica % numero_squadre
        squadra = squadre[indice][1]
        
    elif tipo_squadra == "Squadre Notti Venerdì":
        # Notti venerdì: alternanza S1n/S2n ogni 2 settimane
        inizio_anno = datetime(data.year, 1, 1)
        settimane_dall_inizio = (data - inizio_anno).days // 7
        indice = (settimane_dall_inizio // 2) % numero_squadre
        squadra = squadre[indice][1]
        
    elif tipo_squadra == "Squadre Sere":
        # Sere: rotazione S1-S7 giornaliera
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
    conn = get_conn()
    c = conn.cursor()
    
    # Controlla festività nel database
    c.execute('SELECT 1 FROM festivita WHERE data = ?', (data.strftime('%Y-%m-%d'),))
    festivo = c.fetchone() is not None
    
    # Se non è nel database, controlla giorno settimana
    if not festivo:
        festivo = data.weekday() == 6  # Domenica
    
    conn.close()
    return festivo

def get_chi_tocca_oggi() -> str:
    """
    FUNZIONE PRINCIPALE: Calcola chi tocca oggi per tutti i turni
    """
    oggi = datetime.now()
    domani = oggi + timedelta(days=1)
    
    # Determina tipi di turno per oggi
    turni_oggi = []
    
    # SERA (oggi 20-24)
    if oggi.hour < 20:  # Solo se non è già passata
        if not e_festivo(oggi) and oggi.weekday() != 5:  # Non festivo e non sabato
            squadra_sera = calcola_squadra_di_turno("Squadre Sere", oggi)
            turni_oggi.append(f"🌙 **Sera oggi (20-24):** {squadra_sera}")
    
    # NOTTE (stasera -> domani 24-07)
    if oggi.weekday() == 4:  # Venerdì
        squadra_notte = calcola_squadra_di_turno("Squadre Notti Venerdì", oggi)
    elif oggi.weekday() >= 0 and oggi.weekday() <= 3:  # Lun-Gio
        squadra_notte = calcola_squadra_di_turno("Squadre Notti Feriali", oggi)
    else:  # Sabato e Domenica notte gestite dal weekend
        squadra_notte = "Weekend"
    
    if squadra_notte != "Weekend":
        turni_oggi.append(f"🌃 **Notte stasera (24-07):** {squadra_notte}")
    
    # WEEKEND (se applicabile)
    if oggi.weekday() == 5 or oggi.weekday() == 6 or e_festivo(oggi):  # Sab, Dom o Festivo
        squadra_weekend = calcola_squadra_di_turno("Squadre Weekend", oggi)
        turni_oggi.append(f"🎯 **Weekend/Festivo:** {squadra_weekend}")
    
    # Costruisci messaggio
    if turni_oggi:
        messaggio = "📅 **CHI TOCCA OGGI**\n\n" + "\n".join(turni_oggi)
    else:
        messaggio = "📅 Oggi non ci sono turni programmati"
    
    # Aggiungi info squadre dell'utente
    messaggio += f"\n\n👤 **Le tue squadre:**\n• Weekend: D\n• Notti feriali: Bn\n• Sere: S7"
    
    return messaggio

# === GESTIONE SQUADRE E COMPONENTI ===
def get_squadre_per_tipo(tipo_squadra_id: int) -> List[Tuple]:
    """Ottiene tutte le squadre di un tipo con i componenti"""
    conn = get_conn()
    c = conn.cursor()
    
    c.execute('''
        SELECT s.id, s.nome, s.ordine
        FROM squadre s
        WHERE s.tipo_squadra_id = ?
        ORDER BY s.ordine
    ''', (tipo_squadra_id,))
    squadre = c.fetchall()
    
    risultato = []
    for squadra_id, nome, ordine in squadre:
        # Ottieni componenti ordinati per qualifica e autista
        c.execute('''
            SELECT v.qualifica, v.cognome, v.nome, v.autista
            FROM vvf v
            JOIN squadre_componenti sc ON v.id = sc.vvf_id
            WHERE sc.squadra_id = ?
            ORDER BY 
                CASE v.qualifica 
                    WHEN 'CSV' THEN 1 
                    WHEN 'VV' THEN 2 
                    ELSE 3 
                END,
                CASE v.autista
                    WHEN 'III' THEN 1
                    WHEN 'II' THEN 2  
                    WHEN 'I' THEN 3
                    ELSE 4
                END,
                v.cognome, v.nome
        ''', (squadra_id,))
        componenti = c.fetchall()
        risultato.append((squadra_id, nome, ordine, componenti))
    
    conn.close()
    return risultato

def aggiungi_vvf_a_squadra(vvf_id: int, squadra_id: int) -> bool:
    """Aggiunge un VVF a una squadra"""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO squadre_componenti (squadra_id, vvf_id) VALUES (?, ?)', 
                 (squadra_id, vvf_id))
        conn.commit()
        success = c.rowcount > 0
    except sqlite3.IntegrityError:
        success = False
    finally:
        conn.close()
    return success

def rimuovi_vvf_da_squadra(vvf_id: int, squadra_id: int) -> bool:
    """Rimuove un VVF da una squadra"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM squadre_componenti WHERE squadra_id = ? AND vvf_id = ?', 
             (squadra_id, vvf_id))
    conn.commit()
    success = c.rowcount > 0
    conn.close()
    return success

# === TASTIERA FISICA COMPLETA ===
def crea_tastiera_cambi(user_id: int) -> ReplyKeyboardMarkup:
    """Crea la tastiera fisica completa per gestione cambi"""
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
        await mostra_selezione_vvf_stato(update, context)
        
    elif text == "🔄 Aggiungi Cambio":
        await avvia_wizard_cambio(update, context)
        
    elif text == "🆘 Help Cambi":
        await help_cambi(update, context)
        
    else:
        await update.message.reply_text("ℹ️ Usa i pulsanti per navigare.", 
                                      reply_markup=crea_tastiera_cambi(user_id))

# === GESTIONE SQUADRE - HANDLER COMPLESSI ===
async def mostra_gestione_squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu principale gestione squadre"""
    keyboard = [
        [InlineKeyboardButton("👀 Visualizza Squadre", callback_data="squadre_visualizza")],
        [InlineKeyboardButton("➕ Aggiungi Squadra", callback_data="squadre_aggiungi")],
        [InlineKeyboardButton("🗑️ Rimuovi Squadra", callback_data="squadre_rimuovi")],
        [InlineKeyboardButton("👥 Gestisci Componenti", callback_data="squadre_componenti")],
        [InlineKeyboardButton("📅 Imposta Turni", callback_data="squadre_turni")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🏃‍♂️ **GESTIONE SQUADRE**\n\nScegli un'operazione:",
        reply_markup=reply_markup
    )

async def mostra_visualizza_squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra tutte le squadre organizzate per tipo"""
    conn = get_conn()
    c = conn.cursor()
    
    # Ottieni tutti i tipi squadra
    c.execute('SELECT id, nome, descrizione FROM tipi_squadra ORDER BY id')
    tipi_squadra = c.fetchall()
    
    messaggio = "🏃‍♂️ **ELENCO SQUADRE COMPLETO**\n\n"
    
    for tipo_id, nome_tipo, descrizione in tipi_squadra:
        messaggio += f"**{nome_tipo}** ({descrizione})\n"
        
        squadre_con_componenti = get_squadre_per_tipo(tipo_id)
        for squadra_id, nome_squadra, ordine, componenti in squadre_con_componenti:
            messaggio += f"• **{nome_squadra}:** "
            
            if componenti:
                # Raggruppa per qualifica
                csvs = [f"{cognome} {nome}" for qual, cognome, nome, autista in componenti if qual == 'CSV']
                vv_iii = [f"{cognome} {nome} (III)" for qual, cognome, nome, autista in componenti if qual == 'VV' and autista == 'III']
                vv_ii = [f"{cognome} {nome} (II)" for qual, cognome, nome, autista in componenti if qual == 'VV' and autista == 'II']
                vv_i = [f"{cognome} {nome} (I)" for qual, cognome, nome, autista in componenti if qual == 'VV' and autista == 'I']
                
                if csvs:
                    messaggio += "CSV: " + ", ".join(csvs) + " | "
                if vv_iii:
                    messaggio += "III: " + ", ".join(vv_iii) + " | "
                if vv_ii:
                    messaggio += "II: " + ", ".join(vv_ii) + " | "
                if vv_i:
                    messaggio += "I: " + ", ".join(vv_i)
                    
                # Rimuovi ultimo separatore
                if messaggio.endswith(" | "):
                    messaggio = messaggio[:-3]
            else:
                messaggio += "Nessun componente"
                
            messaggio += "\n"
        messaggio += "\n"
    
    conn.close()
    
    # Split messaggio se troppo lungo
    if len(messaggio) > 4000:
        parti = [messaggio[i:i+4000] for i in range(0, len(messaggio), 4000)]
        for parte in parti:
            await update.callback_query.message.reply_text(parte)
    else:
        await update.callback_query.edit_message_text(messaggio)

# === GESTIONE VVF - HANDLER ===
async def mostra_gestione_vvf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu gestione VVF"""
    keyboard = [
        [InlineKeyboardButton("➕ Aggiungi VVF", callback_data="vvf_aggiungi")],
        [InlineKeyboardButton("🗑️ Rimuovi VVF", callback_data="vvf_rimuovi")],
        [InlineKeyboardButton("👀 Visualizza Tutti", callback_data="vvf_visualizza")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text("👥 **GESTIONE VVF**\n\nScegli un'operazione:", reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text("👥 **GESTIONE VVF**\n\nScegli un'operazione:", reply_markup=reply_markup)

# === WIZARD AGGIUNGI CAMBIO ===
async def avvia_wizard_cambio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avvia il wizard per aggiungere un cambio"""
    # Fase 1: Seleziona data
    oggi = datetime.now()
    keyboard = []
    
    # Genera 7 giorni futuri
    for i in range(7):
        data = oggi + timedelta(days=i)
        keyboard.append([InlineKeyboardButton(
            data.strftime("%d/%m (%a)"),
            callback_data=f"cambio_data_{data.strftime('%Y-%m-%d')}"
        )])
    
    keyboard.append([InlineKeyboardButton("📅 Altra data...", callback_data="cambio_data_custom")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📅 **Seleziona data del cambio:**", reply_markup=reply_markup)

# === HELP COMMAND ===
async def help_cambi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Messaggio di help completo"""
    help_text = """
🆘 **GUIDA BOT GESTIONE CAMBI VVF**

📋 **GESTIONE VVF:**
• **Aggiungi VVF:** Inserisci nuovi volontari (VV/CSV) con qualifica autista
• **Rimuovi VVF:** Elimina volontari dalla lista
• **Visualizza Tutti:** Vedi l'elenco completo

📊 **STATO E BILANCI:**
• **Stato Singolo:** Bilancio ore dettagliato per ogni VVF
• **Prospetto Totale:** Panoramica di tutti i bilanci
• **Carichi Pendenti:** Cambi programmati ma non effettuati
• **Mie Sostituzioni:** Cambi futuri dove sei coinvolto

🔄 **GESTIONE CAMBI:**
• **Aggiungi Cambio:** Wizard guidato per inserire cambi
• **Rimuovi Cambio:** Cancella cambi inseriti per errore

📅 **SISTEMA SQUADRE:**
• **Chi Tocca:** Visualizza turni di oggi/domani
• **Visualizza Squadre:** Elenco completo con componenti
• **Gestisci Componenti:** Assegna VVF alle squadre
• **Imposta Turni:** Configura rotazioni automatiche

🎯 **Il sistema calcola automaticamente:**
- Bilancio ore (dato vs ricevuto)
- Rotazione squadre
- Compatibilità autisti
- Gestione festività
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
        
    elif data == "squadre_aggiungi":
        await mostra_aggiungi_squadra(update, context)
        
    elif data.startswith("cambio_data_"):
        await gestisci_selezione_data_cambio(update, context)
        
    elif data == "vvf_visualizza":
        await mostra_tutti_vvf(update, context)
        
    elif data == "vvf_aggiungi":
        await avvia_wizard_aggiungi_vvf(update, context)

# === FUNZIONI AUSILIARIE ===
async def mostra_aggiungi_squadra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interfaccia per aggiungere nuova squadra"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT id, nome FROM tipi_squadra')
    tipi = c.fetchall()
    conn.close()
    
    keyboard = []
    for tipo_id, nome_tipo in tipi:
        keyboard.append([InlineKeyboardButton(nome_tipo, callback_data=f"aggiungi_squadra_tipo_{tipo_id}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "🏃‍♂️ **AGGIUNGI NUOVA SQUADRA**\n\nSeleziona il tipo di squadra:",
        reply_markup=reply_markup
    )

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
    
    # Raggruppa per qualifica e autista
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

# === SISTEMA BACKUP (SIMILE AL PRIMO BOT) ===
def backup_database_cambi():
    """Backup del database cambi su GitHub Gist"""
    if not GITHUB_TOKEN:
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
        
        if GIST_ID_CAMBI:
            url = f'https://api.github.com/gists/{GIST_ID_CAMBI}'
            response = requests.patch(url, headers=headers, json={'files': files})
        else:
            url = 'https://api.github.com/gists'
            data = {
                'description': f'Backup Cambi VVF - {datetime.now().strftime("%Y-%m-%d %H:%M")}',
                'public': False,
                'files': files
            }
            response = requests.post(url, headers=headers, json=data)
        
        if response.status_code in [200, 201]:
            print("✅ Backup cambi completato")
            return True
        return False
        
    except Exception as e:
        print(f"❌ Errore backup cambi: {e}")
        return False

def backup_scheduler_cambi():
    """Scheduler backup per database cambi"""
    while True:
        time.sleep(1800)  # 30 minuti
        backup_database_cambi()

# === KEEP-ALIVE SYSTEM ===
def keep_alive_cambi():
    """Keep-alive per il bot cambi"""
    urls = [
        "https://telegram-bot-cambi.onrender.com/health",
        "https://telegram-bot-cambi.onrender.com/"
    ]
    
    while True:
        for url in urls:
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    print(f"✅ Ping Cambi - {datetime.now().strftime('%H:%M:%S')}")
                else:
                    print(f"⚠️  Ping Cambi fallito - Status: {response.status_code}")
            except Exception as e:
                print(f"❌ Errore ping Cambi: {e}")
        time.sleep(300)

# === WEB SERVER ===
app_cambi = Flask(__name__)

@app_cambi.route('/')
def home_cambi():
    return "🤖 Bot Gestione Cambi VVF - ONLINE 🟢"

@app_cambi.route('/health')
def health_cambi():
    return "OK"

@app_cambi.route('/ping')
def ping_cambi():
    return f"PONG CAMBI - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

def run_flask_cambi():
    app_cambi.run(host='0.0.0.0', port=10001, debug=False)

# === MAIN ===
def main_cambi():
    """Funzione principale del bot cambi"""
    print("🚀 Avvio Bot Gestione Cambi VVF...")
    
    # Avvia web server
    flask_thread = threading.Thread(target=run_flask_cambi, daemon=True)
    flask_thread.start()
    
    # Avvia keep-alive
    keep_alive_thread = threading.Thread(target=keep_alive_cambi, daemon=True)
    keep_alive_thread.start()
    
    # Avvia backup scheduler
    backup_thread = threading.Thread(target=backup_scheduler_cambi, daemon=True)
    backup_thread.start()
    
    # Configura bot Telegram
    application = Application.builder().token(BOT_TOKEN_CAMBI).build()
    
    # Aggiungi handler
    application.add_handler(CommandHandler("start", start_cambi))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_cambi))
    application.add_handler(CallbackQueryHandler(button_handler_cambi))
    
    print("🤖 Bot Gestione Cambi VVF Avviato!")
    print("📍 Server: Render.com (Porta 10001)")
    print("👤 Utilizzatore: Solo user ID", MY_USER_ID)
    
    application.run_polling()

if __name__ == '__main__':
    main_cambi()