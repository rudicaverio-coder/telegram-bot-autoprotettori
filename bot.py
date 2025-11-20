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

# SOGLIE BOMBOLE (ORA SONO COMBINATE ERBA + CENTRALE)
SOGLIE_BOMBOLE = {
    "sotto_scorta": 7,      # <8 (TOTALE Erba + Centrale)
    "allarme_scorta": 8,    # =8 (TOTALE Erba + Centrale)  
    "preallarme": 10        # =10 (TOTALE Erba + Centrale)
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

# === SISTEMA DI EMERGENZA PER RICREARE TABELLE ===
def emergency_recreate_database():
    """Ricrea le tabelle se non esistono - sistema di emergenza"""
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    
    try:
        c.execute("SELECT 1 FROM articoli LIMIT 1")
        c.execute("SELECT 1 FROM utenti LIMIT 1")
        print("✅ Tabelle database verificate")
    except sqlite3.OperationalError:
        print("🚨 TABELLE NON TROVATE! Ricreo il database di emergenza...")
        init_db()
        print("✅ Database ricreato con successo!")
    
    conn.close()

# === VERIFICA INTEGRITÀ DATABASE ===
def check_database_integrity():
    """Verifica che il database sia integro e funzionante"""
    try:
        conn = sqlite3.connect(DATABASE_NAME)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('articoli', 'utenti')")
        table_count = c.fetchone()[0]
        
        if table_count < 2:
            print("🚨 Database corrotto - tabelle mancanti!")
            conn.close()
            return False
            
        c.execute("SELECT COUNT(*) FROM articoli")
        articoli_count = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM utenti WHERE ruolo = 'admin'")
        admin_count = c.fetchone()[0]
        
        print(f"✅ Database integro - Tabelle: {table_count}, Articoli: {articoli_count}, Admin: {admin_count}")
        conn.close()
        return True
        
    except Exception as e:
        print(f"🚨 Errore verifica database: {e}")
        return False

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

STATI_CENTRALE = {
    "usato_centrale": "🔴 Usato (Centrale)",
    "fuori_uso_centrale": "⚫ Fuori Uso (Centrale)"
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

def ripristina_da_centrale(seriale):
    """Ripristina un articolo da centrale a Erba"""
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    
    c.execute("SELECT stato FROM articoli WHERE seriale = ?", (seriale,))
    risultato = c.fetchone()
    
    if risultato:
        stato_attuale = risultato[0]
        nuovo_stato = ""
        
        if stato_attuale == "usato_centrale":
            nuovo_stato = "usato"
        elif stato_attuale == "fuori_uso_centrale":
            nuovo_stato = "fuori_uso"
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

# === SISTEMA BACKUP AUTOMATICO SU GITHUB ===
def backup_database_to_gist():
    """Salva il database su GitHub Gist"""
    if not GITHUB_TOKEN:
        print("❌ Token GitHub non configurato - backup disabilitato")
        return False
    
    try:
        with open(DATABASE_NAME, 'rb') as f:
            db_content = f.read()
        
        db_base64 = base64.b64encode(db_content).decode('utf-8')
        
        files = {
            'autoprotettori_backup.json': {
                'content': json.dumps({
                    'timestamp': datetime.now().isoformat(),
                    'database_size': len(db_content),
                    'database_base64': db_base64,
                    'backup_type': 'automatic'
                })
            }
        }
        
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        if GIST_ID:
            url = f'https://api.github.com/gists/{GIST_ID}'
            data = {'files': files}
            response = requests.patch(url, headers=headers, json=data)
        else:
            url = 'https://api.github.com/gists'
            data = {
                'description': f'Backup Autoprotettori Bot - {datetime.now().strftime("%Y-%m-%d %H:%M")}',
                'public': False,
                'files': files
            }
            response = requests.post(url, headers=headers, json=data)
        
        if response.status_code in [200, 201]:
            result = response.json()
            print(f"✅ Backup su Gist completato: {result['html_url']}")
            
            if not GIST_ID:
                with open('gist_id.txt', 'w') as f:
                    f.write(result['id'])
                print(f"📝 Nuovo Gist ID salvato: {result['id']}")
            
            return True
        else:
            print(f"❌ Errore backup Gist: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Errore durante backup: {str(e)}")
        return False

def restore_database_from_gist():
    """Ripristina il database da GitHub Gist"""
    if not GITHUB_TOKEN or not GIST_ID:
        print("❌ Token o Gist ID non configurati - restore disabilitato")
        return False
    
    try:
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        url = f'https://api.github.com/gists/{GIST_ID}'
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            gist_data = response.json()
            backup_file = gist_data['files'].get('autoprotettori_backup.json')
            
            if backup_file:
                backup_content = json.loads(backup_file['content'])
                db_base64 = backup_content['database_base64']
                timestamp = backup_content['timestamp']
                
                db_content = base64.b64decode(db_base64)
                with open(DATABASE_NAME, 'wb') as f:
                    f.write(db_content)
                
                print(f"✅ Database ripristinato da backup: {timestamp}")
                return True
            else:
                print("❌ File di backup non trovato nel Gist")
                return False
        else:
            print(f"❌ Errore recupero Gist: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Errore durante restore: {str(e)}")
        return False

def restore_on_startup():
    """Tenta il ripristino del database all'avvio"""
    if not GITHUB_TOKEN or not GIST_ID:
        print("❌ Token o Gist ID non configurati - restore disabilitato")
        return False
    
    print("🔄 Tentativo di ripristino database da backup...")
    if restore_database_from_gist():
        print("✅ Database ripristinato dal backup GitHub!")
        return True
    else:
        print("❌ Ripristino fallito, si parte con database nuovo")
        init_db()
        return False

# === BACKUP AUTOMATICO OGNI 25 MINUTI ===
def backup_scheduler():
    """Scheduler per backup automatici migliorato"""
    print("🔄 Scheduler backup avviato (ogni 25 minuti)")
    
    time.sleep(10)
    print("🔄 Backup iniziale in corso...")
    backup_database_to_gist()
    
    while True:
        time.sleep(1500)
        print("🔄 Backup automatico in corso...")
        if backup_database_to_gist():
            print("✅ Backup completato con successo")
        else:
            print("❌ Backup fallito, riprovo al prossimo ciclo")

# === SISTEMA KEEP-ALIVE ULTRA-AGGRESSIVO ===
def keep_alive_aggressive():
    """Keep-alive ultra-aggressivo per evitare spin-down"""
    urls = [
        "https://telegram-bot-autoprotettori.onrender.com/health",
        "https://telegram-bot-autoprotettori.onrender.com/", 
        "https://telegram-bot-autoprotettori.onrender.com/ping",
        "https://telegram-bot-autoprotettori.onrender.com/status",
        "https://telegram-bot-autoprotettori.onrender.com/keep-alive"
    ]
    
    print("🔄 Sistema keep-alive ULTRA-AGGRESSIVO avviato! Ping ogni 5 minuti...")
    
    while True:
        success_count = 0
        for url in urls:
            try:
                response = requests.get(url, timeout=15)
                if response.status_code == 200:
                    print(f"✅ Ping riuscito - {datetime.now().strftime('%H:%M:%S')} - {url}")
                    success_count += 1
                else:
                    print(f"⚠️  Ping {url} - Status: {response.status_code}")
            except Exception as e:
                print(f"❌ Errore ping {url}: {e}")
        
        print(f"📊 Ping completati: {success_count}/{len(urls)} successi")
        
        if success_count == 0:
            print("🚨 CRITICO: Tutti i ping falliti! Riavvio in 30 secondi...")
            time.sleep(30)
            os._exit(1)
        
        time.sleep(300)

# === FUNZIONI SERVER STATUS ===
def get_render_usage_simple():
    """Versione semplificata che stima l'uso basandosi sul tempo di attività"""
    try:
        today = datetime.now()
        first_day_next_month = datetime(today.year, today.month % 12 + 1, 1)
        last_day_current_month = first_day_next_month - timedelta(days=1)
        days_in_month = last_day_current_month.day
        days_passed = today.day
        days_remaining = days_in_month - days_passed
        
        hours_in_day = 24
        estimated_hours_used = days_passed * hours_in_day
        monthly_limit = 750
        
        projected_monthly_usage = (estimated_hours_used / days_passed) * days_in_month
        hours_remaining = monthly_limit - projected_monthly_usage
        
        usage_percentage = (estimated_hours_used / monthly_limit) * 100
        projected_percentage = (projected_monthly_usage / monthly_limit) * 100
        
        status_msg = "🖥️ **STATUS SERVER RENDER**\n\n"
        status_msg += f"📅 **MESE CORRENTE:** {today.strftime('%B %Y')}\n"
        status_msg += f"• Giorni passati: {days_passed}/{days_in_month}\n"
        status_msg += f"• Giorni rimanenti: {days_remaining}\n\n"
        
        status_msg += "⏰ **CONSUMO ORE (STIMA):**\n"
        status_msg += f"• Ore stimate usate: {estimated_hours_used:.1f}h\n"
        status_msg += f"• Proiezione mensile: {projected_monthly_usage:.1f}h/750h\n"
        status_msg += f"• Ore stimate rimanenti: {hours_remaining:.1f}h\n\n"
        
        status_msg += "📊 **PERCENTUALI:**\n"
        status_msg += f"• Consumo attuale: {usage_percentage:.1f}%\n"
        status_msg += f"• Proiezione finale: {projected_percentage:.1f}%\n\n"
        
        if projected_percentage > 80:
            status_msg += "🚨 **ATTENZIONE:** Consumo elevato previsto!\n"
        elif projected_percentage > 60:
            status_msg += "⚠️ **NOTA:** Consumo nella norma\n"
        else:
            status_msg += "✅ **OK:** Consumo sotto controllo\n"
            
        status_msg += f"\n🕒 Aggiornato: {today.strftime('%d/%m/%Y %H:%M')}"
        
        return status_msg
        
    except Exception as e:
        return f"❌ Errore nel calcolo: {str(e)}"

def get_system_metrics():
    """Ottiene metriche di sistema base"""
    try:
        process = psutil.Process(os.getpid())
        process_memory = process.memory_info().rss / 1024 / 1024
        
        system_memory = psutil.virtual_memory()
        total_memory_used = system_memory.used / 1024 / 1024
        total_memory_total = system_memory.total / 1024 / 1024
        memory_percent = system_memory.percent
        
        cpu_percent = psutil.cpu_percent(interval=1)
        
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
        
        metrics_msg = "📊 **METRICHE DI SISTEMA:**\n"
        metrics_msg += f"• RAM Bot: {process_memory:.1f}MB\n"
        metrics_msg += f"• RAM Sistema: {total_memory_used:.1f}MB / {total_memory_total:.1f}MB ({memory_percent:.1f}%)\n"
        metrics_msg += f"• CPU: {cpu_percent:.1f}%\n"
        metrics_msg += f"• Uptime: {str(uptime).split('.')[0]}\n"
        
        return metrics_msg
        
    except Exception as e:
        return f"📊 Errore metriche: {str(e)}"

# === FUNZIONI ARTICOLI ===
def get_prefisso_categoria(categoria):
    """Restituisce il prefisso automatico per ogni categoria"""
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

def get_articolo(seriale):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM articoli WHERE seriale = ?", (seriale,))
    result = c.fetchone()
    conn.close()
    return result

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

def get_articoli_per_categoria(categoria):
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute("SELECT seriale, categoria, sede, stato FROM articoli WHERE categoria = ?", (categoria,))
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
    """CONTA TOTALE BOMBOLE (Erba + Centrale) - NUOVA VERSIONE"""
    conn = sqlite3.connect(DATABASE_NAME)
    c = conn.cursor()
    c.execute('''SELECT COUNT(*) FROM articoli 
                 WHERE categoria = 'bombola' AND stato = 'disponibile' ''')
    risultato = c.fetchone()[0]
    conn.close()
    return risultato

def get_categorie_con_articoli(stato=None):
    """Restituisce le categorie che hanno articoli in un determinato stato"""
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
    """Organizza gli articoli per categoria nell'ordine prestabilito"""
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

# === NUOVA FUNZIONE: RICOSTRUISCI DATABASE DA INVENTARIO ===
def ricostruisci_database_da_inventario(testo_inventario):
    """Ricostruisce il database dal testo dell'inventario"""
    try:
        conn = sqlite3.connect(DATABASE_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM articoli")
        
        mappatura_categorie = {
            "⚗️ Bombola": "bombola",
            "🎭 Maschera": "maschera", 
            "💨 Erogatore": "erogatore",
            "🎽 Spallaccio": "spallaccio",
            "🏠 Seconda Utenza": "seconda_utenza"
        }
        
        mappatura_sedi = {
            "🌿 Erba": "erba",
            "🏢 Centrale": "centrale"
        }
        
        mappatura_stati = {
            "🟢 DISPONIBILI": "disponibile",
            "🔴 USATI": "usato",
            "⚫ FUORI USO": "fuori_uso"
        }
        
        lines = testo_inventario.split('\n')
        categoria_corrente = None
        stato_corrente = None
        articoli_inseriti = 0
        errori = 0
        articoli_invalidi = []
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            for stato_testo, stato_db in mappatura_stati.items():
                if stato_testo in line:
                    stato_corrente = stato_db
                    categoria_corrente = None
                    break
            
            for cat_testo, cat_db in mappatura_categorie.items():
                if cat_testo in line and ":" in line:
                    categoria_corrente = cat_db
                    break
            
            if line.startswith('•') and categoria_corrente and stato_corrente:
                try:
                    if ' - ' in line:
                        parts = line.split(' - ')[0]
                        seriale = parts[2:].strip()
                    else:
                        seriale = line[2:].strip()
                    
                    sede_trovata = None
                    for sede_testo, sede_db in mappatura_sedi.items():
                        if sede_testo in line:
                            sede_trovata = sede_db
                            break
                    
                    if not sede_trovata:
                        if seriale.endswith('_ERBA'):
                            sede_trovata = 'erba'
                        elif seriale.endswith('_CENTRALE'):
                            sede_trovata = 'centrale'
                    
                    if sede_trovata and seriale:
                        stato_finale = stato_corrente
                        if " (Centrale)" in line:
                            if stato_corrente == "usato":
                                stato_finale = "usato_centrale"
                            elif stato_corrente == "fuori_uso":
                                stato_finale = "fuori_uso_centrale"
                        
                        c.execute('''INSERT OR IGNORE INTO articoli (seriale, categoria, sede, stato) 
                                     VALUES (?, ?, ?, ?)''', (seriale, categoria_corrente, sede_trovata, stato_finale))
                        
                        if c.rowcount > 0:
                            articoli_inseriti += 1
                        else:
                            errori += 1
                    else:
                        errori += 1
                        articoli_invalidi.append(f"{seriale} (sede non trovata)")
                            
                except Exception as e:
                    errori += 1
                    articoli_invalidi.append(line)
        
        conn.commit()
        conn.close()
        
        messaggio = f"✅ Database ricostruito con successo!\n• Articoli inseriti: {articoli_inseriti}\n• Errori/duplicati: {errori}"
        
        if articoli_invalidi:
            messaggio += f"\n\n❌ Articoli con problemi (saltati):\n"
            for invalido in articoli_invalidi[:10]:
                messaggio += f"• {invalido}\n"
            if len(articoli_invalidi) > 10:
                messaggio += f"• ... e altri {len(articoli_invalidi) - 10} articoli\n"
        
        return True, messaggio
        
    except Exception as e:
        import traceback
        print(f"🚨 Errore grave durante ricostruzione: {str(e)}")
        print(traceback.format_exc())
        return False, f"❌ Errore durante la ricostruzione: {str(e)}"

# === NUOVE FUNZIONI PER SELEZIONE MULTIPLA ===
async def mostra_selezione_multipla(update, context, tipo_selezione, articoli, titolo, callback_base):
    """Mostra la selezione multipla degli articoli"""
    query = update.callback_query if hasattr(update, 'callback_query') else None
    
    # Inizializza la lista delle selezioni se non esiste
    if f'selezioni_{tipo_selezione}' not in context.user_data:
        context.user_data[f'selezioni_{tipo_selezione}'] = []
    
    # Salva gli articoli disponibili nel context
    context.user_data[f'articoli_{tipo_selezione}'] = articoli
    
    # Crea tastiera con selezione multipla
    keyboard = []
    
    for articolo in articoli:
        seriale, categoria, sede = articolo[:3]  # Prende solo i primi 3 elementi
        is_selected = seriale in context.user_data[f'selezioni_{tipo_selezione}']
        
        emoji = "✅" if is_selected else "⚪"
        button_text = f"{emoji} {seriale} - {SEDI[sede]}"
        
        keyboard.append([
            InlineKeyboardButton(button_text, callback_data=f"toggle_{tipo_selezione}_{seriale}")
        ])
    
    # Aggiungi bottoni di controllo
    keyboard.append([
        InlineKeyboardButton("🔄 Deseleziona Tutto", callback_data=f"deseleziona_tutti_{tipo_selezione}"),
        InlineKeyboardButton("✅ Seleziona Tutto", callback_data=f"seleziona_tutti_{tipo_selezione}")
    ])
    
    keyboard.append([
        InlineKeyboardButton("➡️ CONFERMA", callback_data=f"conferma_{tipo_selezione}"),
        InlineKeyboardButton("❌ ANNULLA", callback_data=f"annulla_{tipo_selezione}")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Messaggio con riepilogo
    num_selezionati = len(context.user_data[f'selezioni_{tipo_selezione}'])
    messaggio = f"{titolo}\n\n"
    messaggio += f"🔘 **Articoli selezionati: {num_selezionati}**\n"
    messaggio += "Clicca sui nomi per selezionare/deselezionare\n\n"
    
    if num_selezionati > 0:
        messaggio += "**Articoli selezionati:**\n"
        for seriale in context.user_data[f'selezioni_{tipo_selezione}']:
            messaggio += f"• {seriale}\n"
    
    messaggio += "\nPremi ➡️ CONFERMA quando hai finito"
    
    try:
        if query:
            await query.edit_message_text(messaggio, reply_markup=reply_markup)
        else:
            await update.message.reply_text(messaggio, reply_markup=reply_markup)
    except Exception as e:
        if query:
            await query.message.reply_text(messaggio, reply_markup=reply_markup)
        else:
            await update.message.reply_text(messaggio, reply_markup=reply_markup)

async def gestisci_selezione_multipla(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_data: str):
    """Gestisce la selezione/deselezione degli articoli"""
    query = update.callback_query
    
    if callback_data.startswith("toggle_"):
        # Estrai tipo_selezione e seriale
        parts = callback_data.split('_')
        if len(parts) >= 3:
            tipo_selezione = parts[1]
            seriale = '_'.join(parts[2:])
            
            # Inizializza se non esiste
            if f'selezioni_{tipo_selezione}' not in context.user_data:
                context.user_data[f'selezioni_{tipo_selezione}'] = []
            
            # Toggle della selezione
            if seriale in context.user_data[f'selezioni_{tipo_selezione}']:
                context.user_data[f'selezioni_{tipo_selezione}'].remove(seriale)
            else:
                context.user_data[f'selezioni_{tipo_selezione}'].append(seriale)
            
            # Ricarica la selezione
            articoli = context.user_data.get(f'articoli_{tipo_selezione}', [])
            titolo = get_titolo_selezione(tipo_selezione)
            await mostra_selezione_multipla(update, context, tipo_selezione, articoli, titolo, f"toggle_{tipo_selezione}")
    
    elif callback_data.startswith("seleziona_tutti_"):
        tipo_selezione = callback_data.replace("seleziona_tutti_", "")
        articoli = context.user_data.get(f'articoli_{tipo_selezione}', [])
        
        # Inizializza se non esiste
        if f'selezioni_{tipo_selezione}' not in context.user_data:
            context.user_data[f'selezioni_{tipo_selezione}'] = []
        
        # Seleziona tutti gli articoli disponibili
        context.user_data[f'selezioni_{tipo_selezione}'] = [articolo[0] for articolo in articoli]
        
        titolo = get_titolo_selezione(tipo_selezione)
        await mostra_selezione_multipla(update, context, tipo_selezione, articoli, titolo, f"toggle_{tipo_selezione}")
    
    elif callback_data.startswith("deseleziona_tutti_"):
        tipo_selezione = callback_data.replace("deseleziona_tutti_", "")
        
        # Deseleziona tutti
        context.user_data[f'selezioni_{tipo_selezione}'] = []
        
        articoli = context.user_data.get(f'articoli_{tipo_selezione}', [])
        titolo = get_titolo_selezione(tipo_selezione)
        await mostra_selezione_multipla(update, context, tipo_selezione, articoli, titolo, f"toggle_{tipo_selezione}")
    
    elif callback_data.startswith("conferma_"):
        tipo_selezione = callback_data.replace("conferma_", "")
        await conferma_selezione(update, context, tipo_selezione)
    
    elif callback_data.startswith("annulla_"):
        tipo_selezione = callback_data.replace("annulla_", "")
        await annulla_selezione(update, context, tipo_selezione)

def get_titolo_selezione(tipo_selezione):
    """Restituisce il titolo appropriato per il tipo di selezione"""
    titoli = {
        "usato": "🔴 Seleziona articoli da segnare come USATO",
        "fuoriuso": "⚫ Seleziona articoli da segnare come FUORI USO", 
        "ripristina": "🔄 Seleziona articoli da RIPRISTINARE a DISPONIBILE",
        "rimuovi": "➖ Seleziona articoli da ELIMINARE",
        "centraleusati": "📤 Seleziona USATI da spostare in CENTRALE",
        "centralefuoriuso": "📤 Seleziona FUORI USO da spostare in CENTRALE"
    }
    return titoli.get(tipo_selezione, "Seleziona articoli")

async def conferma_selezione(update: Update, context: ContextTypes.DEFAULT_TYPE, tipo_selezione):
    """Conferma la selezione ed esegue l'azione appropriata"""
    query = update.callback_query
    
    # Inizializza se non esiste
    if f'selezioni_{tipo_selezione}' not in context.user_data:
        context.user_data[f'selezioni_{tipo_selezione}'] = []
        
    selezioni = context.user_data[f'selezioni_{tipo_selezione}']
    
    if not selezioni:
        await query.answer("❌ Nessun articolo selezionato!", show_alert=True)
        return
    
    # Esegui l'azione in base al tipo di selezione
    success_count = 0
    messaggio = ""
    
    if tipo_selezione == "usato":
        for seriale in selezioni:
            update_stato(seriale, "usato")
            success_count += 1
        messaggio = f"✅ {success_count} articoli segnati come USATI!"
    
    elif tipo_selezione == "fuoriuso":
        for seriale in selezioni:
            update_stato(seriale, "fuori_uso")
            success_count += 1
        messaggio = f"✅ {success_count} articoli segnati come FUORI USO!"
    
    elif tipo_selezione == "ripristina":
        for seriale in selezioni:
            update_stato(seriale, "disponibile")
            success_count += 1
        messaggio = f"✅ {success_count} articoli ripristinati a DISPONIBILE!"
    
    elif tipo_selezione == "rimuovi":
        for seriale in selezioni:
            articolo = get_articolo(seriale)
            if articolo:
                delete_articolo(seriale)
                success_count += 1
        messaggio = f"✅ {success_count} articoli eliminati dall'inventario!"
    
    elif tipo_selezione == "centraleusati":
        for seriale in selezioni:
            if sposta_in_centrale(seriale):
                success_count += 1
        messaggio = f"✅ {success_count} articoli USATI spostati in CENTRALE!"
    
    elif tipo_selezione == "centralefuoriuso":
        for seriale in selezioni:
            if sposta_in_centrale(seriale):
                success_count += 1
        messaggio = f"✅ {success_count} articoli FUORI USO spostati in CENTRALE!"
    
    # Pulisci i dati temporanei
    for key in [f'selezioni_{tipo_selezione}', f'articoli_{tipo_selezione}']:
        if key in context.user_data:
            del context.user_data[key]
    
    await query.edit_message_text(messaggio)

async def annulla_selezione(update: Update, context: ContextTypes.DEFAULT_TYPE, tipo_selezione):
    """Annulla la selezione"""
    query = update.callback_query
    
    # Pulisci i dati temporanei
    for key in [f'selezioni_{tipo_selezione}', f'articoli_{tipo_selezione}']:
        if key in context.user_data:
            del context.user_data[key]
    
    await query.edit_message_text(f"❌ Selezione {tipo_selezione} annullata.")

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
• 📤 Caricare inventario per ricostruire database

🔄 **SISTEMA SEMPRE ATTIVO:**
• ✅ Ping automatici ogni 5 minuti
• ✅ Backup automatico ogni 25 minuti
• ✅ Zero tempi di attesa
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
        tastiera.append([KeyboardButton("📤 Carica Inventario")])
        
        if user_id == 1816045269:
            tastiera.append([KeyboardButton("🖥️ Status Server")])

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

# === GESTIONE RICHIESTE ACCESSO UNO ALLA VOLTA ===
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

    # INVENTARIO - NUOVA VERSIONE ORGANIZZATA
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

    # SEGNA USATO - NUOVA VERSIONE CON SELEZIONE MULTIPLA
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
        await update.message.reply_text("🔴 Seleziona categoria per segnare come USATO:", reply_markup=reply_markup)

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

    # FUORI USO - CORRETTO CON SELEZIONE MULTIPLA
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

        # Per admin: CREARE FUORI USO - usa selezione multipla
        articoli_disponibili = get_articoli_per_stato('disponibile')
        articoli_usati = get_articoli_per_stato('usato')
        articoli = articoli_disponibili + articoli_usati

        if not articoli:
            await update.message.reply_text("⚫ Nessun articolo da segnare come fuori uso")
            return

        # Usa la nuova funzione di selezione multipla
        await mostra_selezione_multipla(
            update, 
            context, 
            "fuoriuso", 
            articoli, 
            "⚫ Seleziona articoli da segnare come FUORI USO", 
            "toggle_fuoriuso"
        )

    # AGGIUNGI (solo admin)
    elif text == "➕ Aggiungi" and is_admin(user_id):
        context.user_data['azione'] = 'aggiungi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"nuovo_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("📦 Seleziona categoria:", reply_markup=reply_markup)

    # RIMUOVI (solo admin) - MODIFICATO PER SELEZIONE MULTIPLA
    elif text == "➖ Rimuovi" and is_admin(user_id):
        context.user_data['azione'] = 'rimuovi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"rimuovi_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("➖ Seleziona categoria:", reply_markup=reply_markup)

    # RIPRISTINA (solo admin) - MODIFICATO PER SELEZIONE MULTIPLA
    elif text == "🔄 Ripristina" and is_admin(user_id):
        articoli_usati = get_articoli_per_stato('usato')
        articoli_fuori_uso = get_articoli_per_stato('fuori_uso')
        articoli = articoli_usati + articoli_fuori_uso

        if not articoli:
            await update.message.reply_text("✅ Nessun articolo da ripristinare")
            return

        # Usa la nuova funzione di selezione multipla
        await mostra_selezione_multipla(
            update, 
            context, 
            "ripristina", 
            articoli, 
            "🔄 Seleziona articoli da RIPRISTINARE a DISPONIBILE", 
            "toggle_ripristina"
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

    # CARICA INVENTARIO (solo admin)
    elif text == "📤 Carica Inventario" and is_admin(user_id):
        context.user_data['azione'] = 'carica_inventario'
        await update.message.reply_text(
            "📤 **CARICA INVENTARIO PER RICOSTRUIRE DATABASE**\n\n"
            "Incolla il testo completo dell'inventario (come generato dal bot).\n\n"
            "⚠️ **ATTENZIONE:** Questa operazione SOSTITUIRÀ completamente il database attuale!\n"
            "✅ Assicurati che il testo sia esattamente come generato dal comando '📋 Inventario'.\n\n"
            "Incolla ora il testo dell'inventario:"
        )

    # HELP
    elif text == "🆘 Help":
        await help_command(update, context)

    # IN CENTRALE - CON SELEZIONE MULTIPLA
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

    # STATUS SERVER (SOLO PER ADMIN SPECIFICO)
    elif text == "🖥️ Status Server" and user_id == 1816045269:
        usage_info = get_render_usage_simple()
        system_info = get_system_metrics()
        
        status_msg = f"{usage_info}\n\n{system_info}"
        await update.message.reply_text(status_msg)

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

    # GESTIONE CARICA INVENTARIO
    elif context.user_data.get('azione') == 'carica_inventario':
        if not is_admin(user_id):
            return
            
        testo_inventario = text.strip()
        
        context.user_data['inventario_da_caricare'] = testo_inventario
        context.user_data['azione'] = 'conferma_carica_inventario'
        
        keyboard = [
            [
                InlineKeyboardButton("✅ CONFERMA Ricostruzione", callback_data="conferma_ricostruzione"),
                InlineKeyboardButton("❌ ANNULLA", callback_data="annulla_ricostruzione")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚠️ **CONFERMA RICOSTRUZIONE DATABASE**\n\n"
            "Sei sicuro di voler RICOSTRUIRE il database dall'inventario?\n\n"
            "❌ **TUTTI GLI ARTICOLI ATTUALE SARANNO ELIMINATI!**\n"
            "✅ Verranno ricreati basandosi sul testo dell'inventario.\n\n"
            "Questa operazione è IRREVERSIBILE!",
            reply_markup=reply_markup
        )

    else:
        await update.message.reply_text("ℹ️ Usa i pulsanti per navigare.", reply_markup=crea_tastiera_fisica(user_id))

# === GESTIONE BOTTONI INLINE ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # SEGNA USATO - SELEZIONE CATEGORIA
    if data.startswith("usato_cat_"):
        categoria = data[10:]
        articoli = get_articoli_per_stato('disponibile')
        articoli_categoria = [a for a in articoli if a[1] == categoria]
        
        if not articoli_categoria:
            await query.edit_message_text(f"❌ Nessun articolo disponibile per {CATEGORIE[categoria]}")
            return

        # Usa la nuova funzione di selezione multipla
        await mostra_selezione_multipla(
            update, 
            context, 
            "usato", 
            articoli_categoria, 
            f"🔴 Seleziona {CATEGORIE[categoria]} da segnare come USATO", 
            "toggle_usato"
        )

    # CREA FUORI USO - SELEZIONE CATEGORIA (PER ADMIN)
    elif data.startswith("crea_fuori_uso_cat_"):
        if not is_admin(user_id):
            await query.answer("❌ Solo gli amministratori possono mettere articoli fuori uso!", show_alert=True)
            return
            
        categoria = data[19:]
        articoli_disponibili = get_articoli_per_stato('disponibile')
        articoli_usati = get_articoli_per_stato('usato')
        articoli_categoria = [a for a in articoli_disponibili + articoli_usati if a[1] == categoria]
        
        if not articoli_categoria:
            await query.edit_message_text(f"❌ Nessun articolo per {CATEGORIE[categoria]}")
            return

        # Usa la nuova funzione di selezione multipla
        await mostra_selezione_multipla(
            update, 
            context, 
            "fuoriuso", 
            articoli_categoria, 
            f"⚫ Seleziona {CATEGORIE[categoria]} da segnare come FUORI USO", 
            "toggle_fuoriuso"
        )

    # RIMUOVI - SELEZIONE CATEGORIA
    elif data.startswith("rimuovi_cat_"):
        if not is_admin(user_id):
            await query.answer("❌ Solo gli amministratori possono eliminare articoli!", show_alert=True)
            return
            
        categoria = data[12:]
        articoli = get_articoli_per_stato('disponibile') + get_articoli_per_stato('usato') + get_articoli_per_stato('fuori_uso')
        articoli_categoria = [a for a in articoli if a[1] == categoria]
        
        if not articoli_categoria:
            await query.edit_message_text(f"❌ Nessun articolo per {CATEGORIE[categoria]}")
            return
        
        # Usa la nuova funzione di selezione multipla
        await mostra_selezione_multipla(
            update, 
            context, 
            "rimuovi", 
            articoli_categoria, 
            f"➖ Seleziona articoli da ELIMINARE ({CATEGORIE[categoria]})", 
            "toggle_rimuovi"
        )

    # GESTIONE CENTRALE - SPOSTA USATI (CON SELEZIONE MULTIPLA)
    elif data == "centrale_sposta_usati":
        articoli_usati = get_articoli_per_stato_centrale('usato', escludi_centrale=True)
        if not articoli_usati:
            await query.edit_message_text("❌ Nessun articolo usato da spostare in centrale (o tutti già in centrale)")
            return

        # Usa la nuova funzione di selezione multipla
        await mostra_selezione_multipla(
            update, 
            context, 
            "centraleusati", 
            articoli_usati, 
            "📤 Seleziona USATI da spostare in CENTRALE", 
            "toggle_centraleusati"
        )

    # GESTIONE CENTRALE - SPOSTA FUORI USO (CON SELEZIONE MULTIPLA)
    elif data == "centrale_sposta_fuori_uso":
        articoli_fuori_uso = get_articoli_per_stato_centrale('fuori_uso', escludi_centrale=True)
        if not articoli_fuori_uso:
            await query.edit_message_text("❌ Nessun articolo fuori uso da spostare in centrale (o tutti già in centrale)")
            return

        # Usa la nuova funzione di selezione multipla
        await mostra_selezione_multipla(
            update, 
            context, 
            "centralefuoriuso", 
            articoli_fuori_uso, 
            "📤 Seleziona FUORI USO da spostare in CENTRALE", 
            "toggle_centralefuoriuso"
        )

    # GESTIONE SELEZIONI MULTIPLE
    elif any(data.startswith(prefix) for prefix in ["toggle_", "seleziona_tutti_", "deseleziona_tutti_", "conferma_", "annulla_"]):
        await gestisci_selezione_multipla(update, context, data)

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

    # TORNA AL MENU CENTRALE
    elif data == "centrale_menu":
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
        
        await query.edit_message_text(messaggio, reply_markup=reply_markup)

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

    # GESTIONE RICOSTRUZIONE DATABASE
    elif data == "conferma_ricostruzione":
        if not is_admin(user_id):
            await query.answer("❌ Solo gli amministratori possono ricostruire il database!", show_alert=True)
            return
            
        testo_inventario = context.user_data.get('inventario_da_caricare', '')
        if not testo_inventario:
            await query.edit_message_text("❌ Nessun testo inventario trovato!")
            return
            
        successo, messaggio = ricostruisci_database_da_inventario(testo_inventario)
        
        for key in ['azione', 'inventario_da_caricare']:
            if key in context.user_data:
                del context.user_data[key]
                
        await query.edit_message_text(messaggio)

    elif data == "annulla_ricostruzione":
        for key in ['azione', 'inventario_da_caricare']:
            if key in context.user_data:
                del context.user_data[key]
                
        await query.edit_message_text("❌ Ricostruzione database annullata.")

# === ALLARME BOMBOLE ===
async def controlla_allarme_bombole(context: ContextTypes.DEFAULT_TYPE):
    """Controlla allarme basato su TOTALE bombole (Erba + Centrale)"""
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

# === SERVER FLASK PER RENDER ===
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bot Telegram Autoprotettori - ONLINE 🟢 - Keep-alive attivo!"

@app.route('/health')
def health():
    return "OK"

@app.route('/ping')
def ping():
    return f"PONG - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

@app.route('/status')
def status():
    articoli = len(get_tutti_articoli())
    bombole = conta_bombole_disponibili()
    return f"Bot Active | Articoli: {articoli} | Bombole: {bombole} | Keep-alive: ✅"

@app.route('/keep-alive')
def keep_alive_endpoint():
    return f"KEEP-ALIVE ACTIVE - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

@app.route('/backup-now')
def backup_now():
    """Endpoint per forzare un backup immediato"""
    if backup_database_to_gist():
        return "✅ Backup eseguito con successo!"
    else:
        return "❌ Errore durante il backup"

def run_flask():
    app.run(host='0.0.0.0', port=10000, debug=False)

# === MAIN ===
def main():
    print("🚀 Avvio Bot Autoprotettori Erba...")
    
    if not restore_on_startup():
        print("🔄 Inizializzazione database nuovo...")
        init_db()
    
    print("🔍 Verifica integrità database...")
    if not check_database_integrity():
        print("🔄 Ricreazione database di emergenza...")
        emergency_recreate_database()
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("✅ Flask server started on port 10000")
    
    keep_alive_thread = threading.Thread(target=keep_alive_aggressive, daemon=True)
    keep_alive_thread.start()
    print("✅ Sistema keep-alive ULTRA-AGGRESSIVO attivato! Ping ogni 5 minuti")
    
    backup_thread = threading.Thread(target=backup_scheduler, daemon=True)
    backup_thread.start()
    print("✅ Scheduler backup attivato! Backup ogni 25 minuti")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot Autoprotettori Erba Avviato!")
    print("📍 Server: Render.com")
    print("🟢 Status: ONLINE con keep-alive ultra-aggressivo")
    print("💾 Database: SQLite3 con backup automatico")
    print("👥 Admin configurati:", len(ADMIN_IDS))
    print("⏰ Ping automatici ogni 5 minuti - Zero spin down! 🚀")
    print("💾 Backup automatici ogni 25 minuti - Dati al sicuro! 🛡️")
    print("🏠 Nuova categoria: Seconda Utenza aggiunta!")
    print("📤 Nuova feature: Ricostruzione database da inventario!")
    print("🔄 Nuovo sistema: Selezione multipla con spunte per tutti i flussi!")
    print("🏢 Gestione centrale: Selezione multipla per spostamento articoli!")
    
    application.run_polling()

if __name__ == '__main__':
    main()
