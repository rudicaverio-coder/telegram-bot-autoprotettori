import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from datetime import datetime
import asyncio

# === CONFIGURAZIONE ===
BOT_TOKEN = "INSERISCI_TOKEN"
ADMIN_IDS = [1816045269, 653425963, 693843502, 6622015744]

# SOGLIE CORRETTE PER BOMBOLE
SOGLIE_BOMBOLE = {
    "sotto_scorta": 7,      # <8
    "allarme_scorta": 8,    # =8  
    "preallarme": 10        # =10
}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# === DATABASE MIGLIORATO ===
def init_db():
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS articoli
                 (id INTEGER PRIMARY KEY,
                  seriale TEXT UNIQUE,
                  categoria TEXT,
                  sede TEXT,  -- 'erba' o 'centrale'
                  stato TEXT DEFAULT 'disponibile',
                  data_inserimento TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute('''CREATE TABLE IF NOT EXISTS utenti
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  nome TEXT,
                  ruolo TEXT DEFAULT 'in_attesa',  -- 'admin', 'user', 'in_attesa'
                  data_richiesta TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  data_approvazione TIMESTAMP)''')

    # Inserisci admin
    for admin_id in ADMIN_IDS:
        c.execute('''INSERT OR IGNORE INTO utenti 
                     (user_id, nome, ruolo, data_approvazione) 
                     VALUES (?, 'Admin', 'admin', CURRENT_TIMESTAMP)''', (admin_id,))

    conn.commit()
    conn.close()

init_db()

# === CATEGORIE E SEDI ===
CATEGORIE = {
    "maschera": "🎭 Maschera",
    "erogatore": "💨 Erogatore", 
    "spallaccio": "🎽 Spallaccio",
    "bombola": "⚗️ Bombola"
}

SEDI = {
    "erba": "🌿 Erba",
    "centrale": "🏢 Centrale"
}

# === FUNZIONI UTILITY ===
def is_admin(user_id):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("SELECT ruolo FROM utenti WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result and result[0] == 'admin'

def is_user_approved(user_id):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("SELECT ruolo FROM utenti WHERE user_id = ? AND ruolo IN ('admin', 'user')", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def get_richieste_in_attesa():
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute('''SELECT user_id, username, nome, data_richiesta 
                 FROM utenti WHERE ruolo = 'in_attesa' ORDER BY data_richiesta''')
    result = c.fetchall()
    conn.close()
    return result

def approva_utente(user_id):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute('''UPDATE utenti SET ruolo = 'user', data_approvazione = CURRENT_TIMESTAMP 
                 WHERE user_id = ?''', (user_id,))
    conn.commit()
    conn.close()

# === FUNZIONI ARTICOLI MIGLIORATE ===
def genera_seriale(categoria, sede, id_item):
    """Genera seriale nel formato: Categoria_IDItem_Sede"""
    prefisso = categoria[:3].upper()  # MAS, ERO, SPA, BOM
    return f"{prefisso}_{id_item}_{sede.upper()}"

def insert_articolo(seriale, categoria, sede, stato="disponibile"):
    conn = sqlite3.connect('autoprotettori_v3.db')
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

def get_prossimo_id_item(categoria, sede):
    """Trova il prossimo ID disponibile per una categoria e sede"""
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute('''SELECT seriale FROM articoli 
                 WHERE categoria = ? AND sede = ? 
                 ORDER BY seriale''', (categoria, sede))
    articoli = c.fetchall()
    conn.close()
    
    # Estrai numeri esistenti e trova il prossimo
    numeri_esistenti = []
    for articolo in articoli:
        try:
            parti = articolo[0].split('_')
            if len(parti) >= 2:
                num = int(parti[1])
                numeri_esistenti.append(num)
        except (ValueError, IndexError):
            continue
    
    return max(numeri_esistenti) + 1 if numeri_esistenti else 1

# ... (altre funzioni database simili alle tue ma migliorate)

# === TASTIERA FISICA ===
def crea_tastiera_fisica(user_id):
    if not is_user_approved(user_id):
        return ReplyKeyboardMarkup([[KeyboardButton("🚀 Richiedi Accesso")]], resize_keyboard=True)

    tastiera = [
        [KeyboardButton("📋 Inventario"), KeyboardButton("🔴 Segna Usato")],
        [KeyboardButton("🟢 Disponibili"), KeyboardButton("🔴 Usati")],
        [KeyboardButton("⚫ Fuori Uso"), KeyboardButton("🆘 Help")]
    ]

    if is_admin(user_id):
        tastiera.append([KeyboardButton("➕ Aggiungi"), KeyboardButton("➖ Rimuovi")])
        tastiera.append([KeyboardButton("🔄 Ripristina"), KeyboardButton("📊 Statistiche")])
        tastiera.append([KeyboardButton("👥 Gestisci Richieste")])  # NUOVO!

    return ReplyKeyboardMarkup(tastiera, resize_keyboard=True, persistent=True)

# === HANDLER START CORRETTO ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    # Registra utente se non esiste
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO utenti (user_id, username, nome, ruolo) 
                 VALUES (?, ?, ?, 'in_attesa')''', 
                 (user_id, update.effective_user.username, user_name))
    conn.commit()
    conn.close()

    if not is_user_approved(user_id):
        # Notifica admin della nuova richiesta
        richieste = get_richieste_in_attesa()
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🆕 NUOVA RICHIESTA ACCESSO:\n"
                    f"User: {user_name} (@{update.effective_user.username})\n"
                    f"ID: {user_id}\n"
                    f"Richieste in attesa: {len(richieste)}"
                )
            except:
                pass

        await update.message.reply_text(
            "🎭 **Autoprotettori Erba**\n\n"
            "🔒 La tua richiesta di accesso è stata inviata agli amministratori.\n"
            "Riceverai una notifica non appena verrà approvata.",
            reply_markup=crea_tastiera_fisica(user_id)
        )
        return

    # Utente approvato
    if is_admin(user_id):
        welcome_text = f"🎭 **Autoprotettori Erba**\n\n👨‍💻 Benvenuto ADMIN {user_name}!"
    else:
        welcome_text = f"🎭 **Autoprotettori Erba**\n\n👤 Benvenuto {user_name}!"

    await update.message.reply_text(welcome_text, reply_markup=crea_tastiera_fisica(user_id))

# === GESTIONE RICHIESTE ACCESSO (SOLO ADMIN) ===
async def gestisci_richieste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    richieste = get_richieste_in_attesa()
    if not richieste:
        await update.message.reply_text("✅ Nessuna richiesta di accesso in sospeso.")
        return

    keyboard = []
    for richiesta in richieste:
        user_id_rich, username, nome, data_richiesta = richiesta
        data = data_richiesta.split()[0] if data_richiesta else "N/A"
        testo = f"{nome} (@{username}) - {data}"
        keyboard.append([
            InlineKeyboardButton("✅ Approva", callback_data=f"approva_{user_id_rich}"),
            InlineKeyboardButton("❌ Rifiuta", callback_data=f"rifiuta_{user_id_rich}")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👥 **RICHIESTE ACCESSO IN SOSPESO:**\n\nSeleziona un'azione:",
        reply_markup=reply_markup
    )

# === HANDLER MESSAGGI PRINCIPALE AGGIORNATO ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Gestione richiesta accesso
    if not is_user_approved(user_id):
        if text == "🚀 Richiedi Accesso":
            await start(update, context)
        return

    # ... (altri handler simili ai tuoi ma con logica migliorata)

    # NUOVO: Gestione richieste admin
    elif text == "👥 Gestisci Richieste" and is_admin(user_id):
        await gestisci_richieste(update, context)

    # Gestione aggiunta articolo migliorata
    elif text == "➕ Aggiungi" and is_admin(user_id):
        context.user_data['azione'] = 'aggiungi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"nuovo_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("📦 Seleziona categoria per il nuovo articolo:", reply_markup=reply_markup)

# === GESTIONE BOTTONI INLINE MIGLIORATA ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # GESTIONE APPROVAZIONI
    if data.startswith("approva_"):
        if not is_admin(user_id):
            return
            
        user_id_approvare = int(data[8:])
        approva_utente(user_id_approvare)
        
        # Notifica utente approvato
        try:
            await context.bot.send_message(
                user_id_approvare,
                "✅ **Accesso Approvato!**\n\n"
                "Ora puoi utilizzare tutte le funzioni del bot.\n"
                "Usa /start per iniziare."
            )
        except:
            pass
            
        await query.edit_message_text(f"✅ Utente {user_id_approvare} approvato!")

    elif data.startswith("rifiuta_"):
        if not is_admin(user_id):
            return
            
        user_id_rifiutare = int(data[8:])
        # Elimina utente rifiutato
        conn = sqlite3.connect('autoprotettori_v3.db')
        c = conn.cursor()
        c.execute("DELETE FROM utenti WHERE user_id = ?", (user_id_rifiutare,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"❌ Utente {user_id_rifiutare} rifiutato!")

    # GESTIONE NUOVI ARTICOLI CON SERIALE AUTOMATICO
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

    elif data.startswith("nuovo_sede_"):
        sede = data[11:]
        categoria = context.user_data['nuova_categoria']
        
        # Genera seriale automatico
        prossimo_id = get_prossimo_id_item(categoria, sede)
        seriale = genera_seriale(categoria, sede, prossimo_id)
        
        if insert_articolo(seriale, categoria, sede):
            # Controlla allarme bombole se necessario
            if categoria == 'bombola':
                await controlla_allarme_bombole(context, sede)
                
            await query.edit_message_text(
                f"✅ **Articolo aggiunto!**\n\n"
                f"**Seriale:** {seriale}\n"
                f"**Categoria:** {CATEGORIE[categoria]}\n" 
                f"**Sede:** {SEDI[sede]}\n\n"
                f"Seriale generato automaticamente nel formato richiesto."
            )
        else:
            await query.edit_message_text("❌ Errore nell'aggiunta dell'articolo!")
        
        # Pulisci context
        for key in ['azione', 'nuova_categoria']:
            if key in context.user_data:
                del context.user_data[key]

    # ... (altri handler per usato/fuori uso/ripristina simili ai tuoi)

async def controlla_allarme_bombole(context: ContextTypes.DEFAULT_TYPE, sede=None):
    """Controlla e invia allarmi per le bombole"""
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    
    if sede:
        c.execute('''SELECT COUNT(*) FROM articoli 
                     WHERE categoria = 'bombola' AND sede = ? AND stato = 'disponibile' ''', (sede,))
    else:
        c.execute('''SELECT COUNT(*) FROM articoli 
                     WHERE categoria = 'bombola' AND stato = 'disponibile' ''')
    
    count = c.fetchone()[0]
    conn.close()

    messaggio = None
    if count <= SOGLIE_BOMBOLE["sotto_scorta"]:
        messaggio = f"🚨 **SOTTO SCORTA BOMBOLE!**\nSolo {count} bombole disponibili!"
    elif count == SOGLIE_BOMBOLE["allarme_scorta"]:
        messaggio = f"🟡 **ALLARME SCORTA BOMBOLE**\nSolo {count} bombole disponibili!"
    elif count == SOGLIE_BOMBOLE["preallarme"]:
        messaggio = f"🔶 **PREALLARME SCORTA BOMBOLE**\nSolo {count} bombole disponibili!"

    if messaggio:
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id, messaggio)
            except:
                pass

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot Avviato! Premi Ctrl+C per fermare.")
    application.run_polling()

if __name__ == '__main__':
    main()
