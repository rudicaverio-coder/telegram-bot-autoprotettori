import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from datetime import datetime
import asyncio
import os
from flask import Flask
import threading

# === CONFIGURAZIONE ===
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [1816045269, 653425963, 693843502, 6622015744]

# SOGLIE CORRETTE PER BOMBOLE
SOGLIE_BOMBOLE = {
    "sotto_scorta": 7,      # <8
    "allarme_scorta": 8,    # =8  
    "preallarme": 10        # =10
}

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# === DATABASE ===
def init_db():
    conn = sqlite3.connect('autoprotettori_v3.db')
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

# === FUNZIONI ARTICOLI ===
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

def get_articolo(seriale):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("SELECT * FROM articoli WHERE seriale = ?", (seriale,))
    result = c.fetchone()
    conn.close()
    return result

def update_stato(seriale, stato):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("UPDATE articoli SET stato = ? WHERE seriale = ?", (stato, seriale))
    conn.commit()
    conn.close()

def delete_articolo(seriale):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("DELETE FROM articoli WHERE seriale = ?", (seriale,))
    conn.commit()
    conn.close()

def get_articoli_per_stato(stato):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("SELECT seriale, categoria, sede FROM articoli WHERE stato = ?", (stato,))
    result = c.fetchall()
    conn.close()
    return result

def get_tutti_articoli():
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("SELECT seriale, categoria, sede, stato FROM articoli")
    result = c.fetchall()
    conn.close()
    return result

def conta_bombole_disponibili(sede=None):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    if sede:
        c.execute('''SELECT COUNT(*) FROM articoli 
                     WHERE categoria = 'bombola' AND sede = ? AND stato = 'disponibile' ''', (sede,))
    else:
        c.execute('''SELECT COUNT(*) FROM articoli 
                     WHERE categoria = 'bombola' AND stato = 'disponibile' ''')
    risultato = c.fetchone()[0]
    conn.close()
    return risultato

# === FUNZIONE HELP ===
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_approved(user_id):
        await update.message.reply_text("Per utilizzare il bot, devi prima richiedere l'accesso.", reply_markup=crea_tastiera_fisica(user_id))
        return

    if is_admin(user_id):
        help_text = """
🎭 **GUIDA ADMIN** 👨‍💻

**FUNZIONI:**
• 📋 Inventario - Vista completa
• 🔴 Segna Usato - Marca articoli usati
• 🟢 Disponibili - Solo articoli disponibili
• 🔴 Usati - Solo articoli usati
• ⚫ Fuori Uso - Solo articoli fuori uso
• ➕ Aggiungi - Inserisci nuovo articolo
• ➖ Rimuovi - Elimina articolo
• 🔄 Ripristina - Ripristina articoli usati/fuori uso
• 📊 Statistiche - Statistiche complete
• 👥 Gestisci Richieste - Approva nuovi utenti

**SISTEMA BOMBOLE:**
• 🌿 Bombola Erba
• 🏢 Bombola Centrale
• Allarme automatico scorte basse
"""
    else:
        help_text = """
🎭 **GUIDA UTENTE** 👤

**FUNZIONI:**
• 📋 Inventario - Vista completa
• 🔴 Segna Usato - Marca articoli usati
• 🟢 Disponibili - Solo articoli disponibili
• 🔴 Usati - Solo articoli usati
• ⚫ Fuori Uso - Solo articoli fuori uso

**REGOLA:**
Segna sempre gli articoli dopo l'uso!
"""
    await update.message.reply_text(help_text, reply_markup=crea_tastiera_fisica(user_id))

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
        tastiera.append([KeyboardButton("👥 Gestisci Richieste")])

    return ReplyKeyboardMarkup(tastiera, resize_keyboard=True, is_persistent=True)

# === HANDLER START ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    conn = sqlite3.connect('autoprotettori_v3.db')
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

    if is_admin(user_id):
        welcome_text = f"🎭 **Autoprotettori Erba**\n\n👨‍💻 Benvenuto ADMIN {user_name}!"
    else:
        welcome_text = f"🎭 **Autoprotettori Erba**\n\n👤 Benvenuto {user_name}!"

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
        if disponibili:
            msg += "🟢 **DISPONIBILI:**\n"
            for seriale, cat, sed, stato in disponibili:
                msg += f"• {seriale} - {CATEGORIE[cat]} - {SEDI[sed]}\n"
            msg += "\n"
        
        usati = [a for a in articoli if a[3] == 'usato']
        if usati:
            msg += "🔴 **USATI:**\n"
            for seriale, cat, sed, stato in usati:
                msg += f"• {seriale} - {CATEGORIE[cat]} - {SEDI[sed]}\n"
            msg += "\n"
        
        fuori_uso = [a for a in articoli if a[3] == 'fuori_uso']
        if fuori_uso:
            msg += "⚫ **FUORI USO:**\n"
            for seriale, cat, sed, stato in fuori_uso:
                msg += f"• {seriale} - {CATEGORIE[cat]} - {SEDI[sed]}\n"
        
        await update.message.reply_text(msg)

    # SEGNA USATO
    elif text == "🔴 Segna Usato":
        articoli = get_articoli_per_stato('disponibile')
        if not articoli:
            await update.message.reply_text("✅ Nessun articolo da segnare come usato")
            return

        keyboard = []
        for seriale, cat, sed in articoli:
            nome = f"{seriale} - {CATEGORIE[cat]} - {SEDI[sed]}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"usato_{seriale}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("🔴 Seleziona articolo da segnare come USATO:", reply_markup=reply_markup)

    # DISPONIBILI
    elif text == "🟢 Disponibili":
        articoli = get_articoli_per_stato('disponibile')
        if not articoli:
            await update.message.reply_text("🟢 Nessun articolo disponibile")
            return
        msg = "🟢 **ARTICOLI DISPONIBILI**\n\n"
        for seriale, cat, sed in articoli:
            msg += f"• {seriale} - {CATEGORIE[cat]} - {SEDI[sed]}\n"
        await update.message.reply_text(msg)

    # USATI
    elif text == "🔴 Usati":
        articoli = get_articoli_per_stato('usato')
        if not articoli:
            await update.message.reply_text("🔴 Nessun articolo usato")
            return
        msg = "🔴 **ARTICOLI USATI**\n\n"
        for seriale, cat, sed in articoli:
            msg += f"• {seriale} - {CATEGORIE[cat]} - {SEDI[sed]}\n"
        await update.message.reply_text(msg)

    # FUORI USO
    elif text == "⚫ Fuori Uso":
        articoli_disponibili = get_articoli_per_stato('disponibile')
        articoli_usati = get_articoli_per_stato('usato')
        articoli = articoli_disponibili + articoli_usati

        if not articoli:
            await update.message.reply_text("⚫ Nessun articolo da segnare come fuori uso")
            return

        keyboard = []
        for seriale, cat, sed in articoli:
            nome = f"{seriale} - {CATEGORIE[cat]} - {SEDI[sed]}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"fuori_uso_{seriale}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("⚫ Seleziona articolo da segnare come FUORI USO:", reply_markup=reply_markup)

    # AGGIUNGI (solo admin)
    elif text == "➕ Aggiungi" and is_admin(user_id):
        context.user_data['azione'] = 'aggiungi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"nuovo_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("📦 Seleziona categoria per il nuovo articolo:", reply_markup=reply_markup)

    # RIMUOVI (solo admin)
    elif text == "➖ Rimuovi" and is_admin(user_id):
        context.user_data['azione'] = 'rimuovi'
        await update.message.reply_text("➖ Inserisci il CODICE SERIALE dell'articolo da rimuovere:")

    # RIPRISTINA (solo admin) - ORA FUNZIONA ANCHE PER FUORI USO!
    elif text == "🔄 Ripristina" and is_admin(user_id):
        articoli_usati = get_articoli_per_stato('usato')
        articoli_fuori_uso = get_articoli_per_stato('fuori_uso')
        articoli = articoli_usati + articoli_fuori_uso

        if not articoli:
            await update.message.reply_text("✅ Nessun articolo da ripristinare")
            return

        keyboard = []
        for seriale, cat, sed in articoli:
            stato_attuale = "usato" if (seriale, cat, sed) in articoli_usati else "fuori uso"
            nome = f"{seriale} - {CATEGORIE[cat]} - {SEDI[sed]} ({stato_attuale})"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"ripristina_{seriale}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("🔄 Seleziona articolo da RIPRISTINARE a disponibile:", reply_markup=reply_markup)

    # STATISTICHE (solo admin)
    elif text == "📊 Statistiche" and is_admin(user_id):
        articoli = get_tutti_articoli()
        totale = len(articoli)
        disponibili = len([a for a in articoli if a[3] == 'disponibile'])
        usati = len([a for a in articoli if a[3] == 'usato'])
        fuori_uso = len([a for a in articoli if a[3] == 'fuori_uso'])

        bombole_erba = conta_bombole_disponibili('erba')
        bombole_centrale = conta_bombole_disponibili('centrale')

        msg = "📊 **STATISTICHE COMPLETE**\n\n"
        msg += f"📦 Totale articoli: {totale}\n"
        msg += f"🟢 Disponibili: {disponibili}\n"
        msg += f"🔴 Usati: {usati}\n"
        msg += f"⚫ Fuori uso: {fuori_uso}\n\n"

        msg += "⚗️ **BOMBOLE DISPONIBILI:**\n"
        msg += f"🌿 Erba: {bombole_erba}"
        if bombole_erba < SOGLIE_BOMBOLE["sotto_scorta"]:
            msg += " 🚨 SOTTO SCORTA!"
        elif bombole_erba < SOGLIE_BOMBOLE["scorta_bassa"]:
            msg += " 🟡 Scorta bassa"
        else:
            msg += " ✅ Ok"

        msg += f"\n🏢 Centrale: {bombole_centrale}"
        if bombole_centrale < SOGLIE_BOMBOLE["sotto_scorta"]:
            msg += " 🚨 SOTTO SCORTA!"
        elif bombole_centrale < SOGLIE_BOMBOLE["scorta_bassa"]:
            msg += " 🟡 Scorta bassa"
        else:
            msg += " ✅ Ok"

        await update.message.reply_text(msg)

    # GESTIONE RICHIESTE (solo admin)
    elif text == "👥 Gestisci Richieste" and is_admin(user_id):
        await gestisci_richieste(update, context)

    # HELP
    elif text == "🆘 Help":
        await help_command(update, context)

    # INSERIMENTO CODICE MANUALE
    elif context.user_data.get('azione') == 'inserisci_codice':
        codice = text.upper().strip()
        categoria = context.user_data['categoria_da_aggiungere']
        sede = context.user_data['sede_da_aggiungere']
        
        # Genera seriale con codice manuale + sede
        seriale = f"{codice}_{sede.upper()}"
        
        if insert_articolo(seriale, categoria, sede):
            await update.message.reply_text(
                f"✅ **Articolo aggiunto!**\n\n"
                f"**Seriale:** {seriale}\n"
                f"**Categoria:** {CATEGORIE[categoria]}\n"
                f"**Sede:** {SEDI[sede]}"
            )
            
            # Controlla allarme bombole se necessario
            if categoria == 'bombola':
                await controlla_allarme_bombole(context, sede)
        else:
            await update.message.reply_text(f"❌ {seriale} già esistente!")
        
        # Pulisci context
        for key in ['azione', 'categoria_da_aggiungere', 'sede_da_aggiungere']:
            if key in context.user_data:
                del context.user_data[key]

    # RIMOZIONE ARTICOLO
    elif context.user_data.get('azione') == 'rimuovi':
        seriale = text.upper()
        if get_articolo(seriale):
            delete_articolo(seriale)
            await update.message.reply_text(f"✅ {seriale} rimosso dall'inventario!")
        else:
            await update.message.reply_text(f"❌ {seriale} non trovato!")
        del context.user_data['azione']

    else:
        await update.message.reply_text("ℹ️ Usa i pulsanti per navigare.", reply_markup=crea_tastiera_fisica(user_id))

# === GESTIONE BOTTONI INLINE ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # SEGNA USATO
    if data.startswith("usato_"):
        seriale = data[6:]
        update_stato(seriale, "usato")
        await query.edit_message_text(f"🔴 {seriale} segnato come USATO ✅")

    # SEGNA FUORI USO
    elif data.startswith("fuori_uso_"):
        seriale = data[10:]
        update_stato(seriale, "fuori_uso")
        await query.edit_message_text(f"⚫ {seriale} segnato come FUORI USO ✅")

    # RIPRISTINA (funziona per USATI e FUORI USO!)
    elif data.startswith("ripristina_"):
        seriale = data[11:]
        update_stato(seriale, "disponibile")
        await query.edit_message_text(f"🔄 {seriale} ripristinato a DISPONIBILE ✅")

    # APPROVA UTENTE
    elif data.startswith("approva_"):
        if not is_admin(user_id):
            return
            
        user_id_approvare = int(data[8:])
        approva_utente(user_id_approvare)
        
        try:
            await context.bot.send_message(
                user_id_approvare,
                "✅ **Accesso Approvato!**\n\nOra puoi utilizzare tutte le funzioni del bot.\nUsa /start per iniziare."
            )
        except:
            pass
            
        await query.edit_message_text(f"✅ Utente {user_id_approvare} approvato!")

    # RIFIUTA UTENTE
    elif data.startswith("rifiuta_"):
        if not is_admin(user_id):
            return
            
        user_id_rifiutare = int(data[8:])
        conn = sqlite3.connect('autoprotettori_v3.db')
        c = conn.cursor()
        c.execute("DELETE FROM utenti WHERE user_id = ?", (user_id_rifiutare,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"❌ Utente {user_id_rifiutare} rifiutato!")

    # SELEZIONE CATEGORIA
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

    # SELEZIONE SEDE
    elif data.startswith("nuovo_sede_"):
        sede = data[11:]
        categoria = context.user_data['nuova_categoria']
        
        # Chiedi all'admin di inserire il codice manualmente
        context.user_data['azione'] = 'inserisci_codice'
        context.user_data['categoria_da_aggiungere'] = categoria
        context.user_data['sede_da_aggiungere'] = sede
        
        await query.edit_message_text(
            f"📝 Inserisci il CODICE dell'articolo per {CATEGORIE[categoria]} - {SEDI[sede]}:\n\n"
            f"(Esempio: MAS001, BOM123, ecc.)"
        )

# === ALLARME BOMBOLE ===
async def controlla_allarme_bombole(context: ContextTypes.DEFAULT_TYPE, sede=None):
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

# === SERVER FLASK PER RENDER ===
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bot Telegram is running!"

@app.route('/health')
def health():
    return "OK"

def run_flask():
    app.run(host='0.0.0.0', port=10000, debug=False)

# === MAIN ===
def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    print("🚀 Flask server started on port 10000")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot Avviato! Premi Ctrl+C per fermare.")
    application.run_polling()

if __name__ == '__main__':
    main()
