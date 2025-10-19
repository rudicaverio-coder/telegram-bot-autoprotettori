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

# SOGLIE BOMBOLE
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
def get_prefisso_categoria(categoria):
    """Restituisce il prefisso automatico per ogni categoria"""
    prefissi = {
        "maschera": "MAS",
        "erogatore": "ER", 
        "spallaccio": "SPAL",
        "bombola": "BOMB"
    }
    return prefissi.get(categoria, "ART")

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

def get_articoli_per_categoria(categoria):
    conn = sqlite3.connect('autoprotettori_v3.db')
    c = conn.cursor()
    c.execute("SELECT seriale, categoria, sede, stato FROM articoli WHERE categoria = ?", (categoria,))
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
    
    help_text = """
🤖 **BENVENUTO IN AUTOPROTETTORI ERBA!**

🎯 **COSA PUOI FARE:**

👤 **COME UTENTE:**
• 📋 **Vedere l'inventario** completo
• 🔴 **Segnare articoli usati** dopo l'utilizzo
• 🟢 **Controllare disponibilità** in tempo reale
• 📊 **Monitorare stati** (disponibili/usati/fuori uso)

👨‍💻 **COME ADMIN:**
• ➕ **Aggiungere nuovi articoli** all'inventario
• ➖ **Rimuovere articoli** tramite interfaccia semplice
• 🔄 **Ripristinare articoli** usati o fuori uso
• 📈 **Visualizzare statistiche** dettagliate
• ⚠️ **Ricevere allarmi automatici** per scorte bombole
• 👥 **Gestire richieste accesso** nuovi utenti

🔧 **CARATTERISTICHE TECNICHE:**
• ✅ **Sempre online** 24/7
• ✅ **Interfaccia intuitiva** con pulsanti
• ✅ **Database sicuro** e persistente
• ✅ **Allarmi automatici** per scorte basse
• ✅ **Accesso controllato** e sicuro

⚡ **SISTEMA BOMBOLE INTELLIGENTE:**
• 🌿 **Bombola Erba** - Monitoraggio separato
• 🏢 **Bombola Centrale** - Gestione dedicata
• 🚨 **Allarmi automatici** quando le scorte sono basse

📱 **COME USARE IL BOT:**
1. Usa i pulsanti in basso per navigare
2. Segui sempre gli articoli dopo l'uso
3. Controlla regolarmente le disponibilità

---

🔄 **INFORMAZIONI TECNICHE:**

**COSA SIGNIFICA "SPIN DOWN":**
✅ **Dopo 15 minuti di inattività** il bot si "addormenta"
✅ **Al primo messaggio** si riavvia automaticamente (in 20-30 secondi)
✅ **Non perdi dati** (il database rimane)
✅ **Completamente gratis**

**ESEMPIO PRATICO:**
• **Ora 10:00** - Qualcuno usa il bot ✅
• **Ora 10:15** - Nessun messaggio, il bot si sospende 💤  
• **Ora 12:00** - Arriva un nuovo messaggio ⏰
• **Ora 12:00:30** - Bot si riavvia e risponde ✅

**PERCHÉ VA BENE COMUNQUE:**
I tuoi utenti vedranno solo un piccolo ritardo al primo messaggio dopo inattività
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
    
    # Messaggio di benvenuto migliorato
    welcome_text = f"""
🎭 **AUTOPROTETTORI ERBA** 🤖

Ciao {user_name}! Benvenuto nel sistema di gestione autoprotettori.

📍 **Questo bot ti permette di:**
• Tenere traccia di tutti gli autoprotettori
• Segnare gli articoli usati in tempo reale  
• Controllare le disponibilità istantaneamente
• Monitorare le scorte di bombole

🔒 **Sistema di accesso sicuro:**
• Solo personale autorizzato
• Approvazione richiesta per nuovi utenti
• Differenti permessi per utenti e amministratori

💡 **Usa i pulsanti in basso per iniziare!**
"""
    
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
                    f"🆕 **NUOVA RICHIESTA ACCESSO**\n\n"
                    f"👤 **Utente:** {user_name}\n"
                    f"📱 **Username:** @{update.effective_user.username}\n"
                    f"🆔 **ID:** {user_id}\n"
                    f"📊 **Richieste in attesa:** {len(richieste)}\n\n"
                    f"Usa '👥 Gestisci Richieste' per approvare."
                )
            except:
                pass

        await update.message.reply_text(
            "✅ **Richiesta inviata con successo!**\n\n"
            "La tua richiesta di accesso è stata inviata agli amministratori.\n"
            "Riceverai una notifica non appena verrà approvata.\n\n"
            "⏳ *Tempo di approvazione stimato: pochi minuti*",
            reply_markup=crea_tastiera_fisica(user_id)
        )
        return

    # Utente approvato
    if is_admin(user_id):
        admin_welcome = f"""
👨‍💻 **BENVENUTO ADMIN {user_name}!** 🎉

🔧 **Funzioni amministrative attive:**
• Gestione inventario completa
• Approvazione nuovi utenti
• Statistiche e report
• Sistema allarmi bombole

📋 **Inventario attuale:**
• Articoli totali: {len(get_tutti_articoli())}
• Bombole disponibili: {conta_bombole_disponibili()}
• Richieste in attesa: {len(get_richieste_in_attesa())}

🚀 **Pronto per la gestione!**
"""
        await update.message.reply_text(admin_welcome, reply_markup=crea_tastiera_fisica(user_id))
    else:
        user_welcome = f"""
👤 **BENVENUTO {user_name}!** 🎉

📊 **Stato sistema:**
• Articoli totali: {len(get_tutti_articoli())}
• Bombole disponibili: {conta_bombole_disponibili()}

💡 **Ricorda:**
• Segna sempre gli articoli dopo l'uso
• Controlla le disponibilità prima di prelevare
• Usa i pulsanti per navigare facilmente

✅ **Accesso confermato - Buon lavoro!**
"""
        await update.message.reply_text(user_welcome, reply_markup=crea_tastiera_fisica(user_id))

# === GESTIONE RICHIESTE ACCESSO ===
async def gestisci_richieste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    richieste = get_richieste_in_attesa()
    if not richieste:
        await update.message.reply_text("✅ **Nessuna richiesta di accesso in sospeso.**")
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
        "👥 **RICHIESTE ACCESSO IN SOSPESO**\n\n"
        "Seleziona un'azione per ogni utente:",
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
            await update.message.reply_text("📦 **Inventario vuoto**\n\nNon ci sono ancora articoli registrati.")
            return

        msg = "📋 **INVENTARIO COMPLETO**\n\n"
        
        disponibili = [a for a in articoli if a[3] == 'disponibile']
        if disponibili:
            msg += f"🟢 **DISPONIBILI ({len(disponibili)}):**\n"
            for seriale, cat, sed, stato in disponibili:
                msg += f"• `{seriale}` - {CATEGORIE[cat]} - {SEDI[sed]}\n"
            msg += "\n"
        
        usati = [a for a in articoli if a[3] == 'usato']
        if usati:
            msg += f"🔴 **USATI ({len(usati)}):**\n"
            for seriale, cat, sed, stato in usati:
                msg += f"• `{seriale}` - {CATEGORIE[cat]} - {SEDI[sed]}\n"
            msg += "\n"
        
        fuori_uso = [a for a in articoli if a[3] == 'fuori_uso']
        if fuori_uso:
            msg += f"⚫ **FUORI USO ({len(fuori_uso)}):**\n"
            for seriale, cat, sed, stato in fuori_uso:
                msg += f"• `{seriale}` - {CATEGORIE[cat]} - {SEDI[sed]}\n"
        
        msg += f"\n📊 **Totale articoli:** {len(articoli)}"
        await update.message.reply_text(msg)

    # SEGNA USATO
    elif text == "🔴 Segna Usato":
        articoli = get_articoli_per_stato('disponibile')
        if not articoli:
            await update.message.reply_text("✅ **Nessun articolo da segnare come usato**\n\nTutti gli articoli sono già stati utilizzati o sono fuori uso.")
            return

        keyboard = []
        for seriale, cat, sed in articoli:
            nome = f"{seriale} - {CATEGORIE[cat]} - {SEDI[sed]}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"usato_{seriale}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🔴 **SEGNA COME USATO**\n\n"
            "Seleziona l'articolo che è stato utilizzato:",
            reply_markup=reply_markup
        )

    # DISPONIBILI
    elif text == "🟢 Disponibili":
        articoli = get_articoli_per_stato('disponibile')
        if not articoli:
            await update.message.reply_text("🟢 **Nessun articolo disponibile**\n\nTutti gli articoli sono attualmente in uso o fuori servizio.")
            return
        msg = f"🟢 **ARTICOLI DISPONIBILI ({len(articoli)})**\n\n"
        for seriale, cat, sed in articoli:
            msg += f"• `{seriale}` - {CATEGORIE[cat]} - {SEDI[sed]}\n"
        await update.message.reply_text(msg)

    # USATI
    elif text == "🔴 Usati":
        articoli = get_articoli_per_stato('usato')
        if not articoli:
            await update.message.reply_text("🔴 **Nessun articolo usato**\n\nNon ci sono articoli segnati come usati.")
            return
        msg = f"🔴 **ARTICOLI USATI ({len(articoli)})**\n\n"
        for seriale, cat, sed in articoli:
            msg += f"• `{seriale}` - {CATEGORIE[cat]} - {SEDI[sed]}\n"
        await update.message.reply_text(msg)

    # FUORI USO
    elif text == "⚫ Fuori Uso":
        articoli_disponibili = get_articoli_per_stato('disponibile')
        articoli_usati = get_articoli_per_stato('usato')
        articoli = articoli_disponibili + articoli_usati

        if not articoli:
            await update.message.reply_text("⚫ **Nessun articolo da segnare come fuori uso**\n\nNon ci sono articoli disponibili per questa operazione.")
            return

        keyboard = []
        for seriale, cat, sed in articoli:
            nome = f"{seriale} - {CATEGORIE[cat]} - {SEDI[sed]}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"fuori_uso_{seriale}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚫ **SEGNA COME FUORI USO**\n\n"
            "Seleziona l'articolo da mettere fuori uso:",
            reply_markup=reply_markup
        )

    # AGGIUNGI (solo admin)
    elif text == "➕ Aggiungi" and is_admin(user_id):
        context.user_data['azione'] = 'aggiungi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"nuovo_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📦 **AGGIUNGI NUOVO ARTICOLO**\n\n"
            "Seleziona la categoria:",
            reply_markup=reply_markup
        )

    # RIMUOVI (solo admin) - NUOVA VERSIONE CON BOTTONI
    elif text == "➖ Rimuovi" and is_admin(user_id):
        context.user_data['azione'] = 'rimuovi_categoria'
        keyboard = [
            [InlineKeyboardButton(CATEGORIE[cat], callback_data=f"rimuovi_cat_{cat}")] 
            for cat in CATEGORIE
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "➖ **RIMUOVI ARTICOLO**\n\n"
            "Seleziona categoria dell'articolo da rimuovere:",
            reply_markup=reply_markup
        )

    # RIPRISTINA (solo admin)
    elif text == "🔄 Ripristina" and is_admin(user_id):
        articoli_usati = get_articoli_per_stato('usato')
        articoli_fuori_uso = get_articoli_per_stato('fuori_uso')
        articoli = articoli_usati + articoli_fuori_uso

        if not articoli:
            await update.message.reply_text("✅ **Nessun articolo da ripristinare**\n\nTutti gli articoli sono già disponibili.")
            return

        keyboard = []
        for seriale, cat, sed in articoli:
            stato_attuale = "usato" if (seriale, cat, sed) in articoli_usati else "fuori uso"
            nome = f"{seriale} - {CATEGORIE[cat]} ({stato_attuale})"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"ripristina_{seriale}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🔄 **RIPRISTINA ARTICOLO**\n\n"
            "Seleziona l'articolo da ripristinare a disponibile:",
            reply_markup=reply_markup
        )

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
        msg += f"📦 **Totale articoli:** {totale}\n"
        msg += f"🟢 **Disponibili:** {disponibili}\n"
        msg += f"🔴 **Usati:** {usati}\n"
        msg += f"⚫ **Fuori uso:** {fuori_uso}\n\n"

        msg += "⚗️ **BOMBOLE DISPONIBILI:**\n"
        msg += f"🌿 **Erba:** {bombole_erba}"
        if bombole_erba < SOGLIE_BOMBOLE["sotto_scorta"]:
            msg += " 🚨 **SOTTO SCORTA!**"
        elif bombole_erba < SOGLIE_BOMBOLE["scorta_bassa"]:
            msg += " 🟡 **Scorta bassa**"
        else:
            msg += " ✅ **Ok**"

        msg += f"\n🏢 **Centrale:** {bombole_centrale}"
        if bombole_centrale < SOGLIE_BOMBOLE["sotto_scorta"]:
            msg += " 🚨 **SOTTO SCORTA!**"
        elif bombole_centrale < SOGLIE_BOMBOLE["scorta_bassa"]:
            msg += " 🟡 **Scorta bassa**"
        else:
            msg += " ✅ **Ok**"

        await update.message.reply_text(msg)

    # GESTIONE RICHIESTE (solo admin)
    elif text == "👥 Gestisci Richieste" and is_admin(user_id):
        await gestisci_richieste(update, context)

    # HELP
    elif text == "🆘 Help":
        await help_command(update, context)

    # INSERIMENTO NUMERO (NUOVA VERSIONE)
    elif context.user_data.get('azione') == 'inserisci_numero':
        numero = text.strip()
        categoria = context.user_data['categoria_da_aggiungere']
        sede = context.user_data['sede_da_aggiungere']
        
        # Verifica che sia un numero
        if not numero.isdigit():
            await update.message.reply_text("❌ **Errore:** Inserisci solo numeri! Riprova:")
            return
        
        # Genera seriale automatico con prefisso + numero + sede
        prefisso = get_prefisso_categoria(categoria)
        seriale = f"{prefisso}_{numero}_{sede.upper()}"
        
        if insert_articolo(seriale, categoria, sede):
            await update.message.reply_text(
                f"✅ **ARTICOLO AGGIUNTO!**\n\n"
                f"**Seriale:** `{seriale}`\n"
                f"**Categoria:** {CATEGORIE[categoria]}\n"
                f"**Sede:** {SEDI[sede]}\n\n"
                f"*Il codice è stato generato automaticamente*"
            )
            
            # Controlla allarme bombole se necessario
            if categoria == 'bombola':
                await controlla_allarme_bombole(context, sede)
        else:
            await update.message.reply_text(
                f"❌ **ERRORE:** `{seriale}` già esistente!\n"
                f"Prova con un numero diverso."
            )
        
        # Pulisci context
        for key in ['azione', 'categoria_da_aggiungere', 'sede_da_aggiungere']:
            if key in context.user_data:
                del context.user_data[key]

    else:
        await update.message.reply_text(
            "ℹ️ **Usa i pulsanti in basso per navigare.**\n\n"
            "Se hai bisogno di aiuto, clicca su '🆘 Help'",
            reply_markup=crea_tastiera_fisica(user_id)
        )

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
        await query.edit_message_text(f"🔴 **{seriale} segnato come USATO** ✅")

    # SEGNA FUORI USO
    elif data.startswith("fuori_uso_"):
        seriale = data[10:]
        update_stato(seriale, "fuori_uso")
        await query.edit_message_text(f"⚫ **{seriale} segnato come FUORI USO** ✅")

    # RIPRISTINA
    elif data.startswith("ripristina_"):
        seriale = data[11:]
        update_stato(seriale, "disponibile")
        await query.edit_message_text(f"🔄 **{seriale} ripristinato a DISPONIBILE** ✅")

    # APPROVA UTENTE
    elif data.startswith("approva_"):
        if not is_admin(user_id):
            return
            
        user_id_approvare = int(data[8:])
        approva_utente(user_id_approvare)
        
        try:
            await context.bot.send_message(
                user_id_approvare,
                "✅ **ACCESSO APPROVATO!** 🎉\n\n"
                "Ora puoi utilizzare tutte le funzioni del bot.\n"
                "Usa /start per iniziare con le funzioni complete.\n\n"
                "📱 *Buon lavoro!*"
            )
        except:
            pass
            
        await query.edit_message_text(f"✅ **Utente {user_id_approvare} approvato!**")

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
        await query.edit_message_text(f"❌ **Utente {user_id_rifiutare} rifiutato!**")

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
        await query.edit_message_text(
            f"🏢 **SELEZIONA SEDE**\n\n"
            f"Articolo: {CATEGORIE[categoria]}\n"
            f"Scegli la sede:",
            reply_markup=reply_markup
        )

    # SELEZIONE SEDE PER AGGIUNTA
    elif data.startswith("nuovo_sede_"):
        sede = data[11:]
        categoria = context.user_data['nuova_categoria']
        
        # Chiedi all'admin di inserire solo il NUMERO
        context.user_data['azione'] = 'inserisci_numero'
        context.user_data['categoria_da_aggiungere'] = categoria
        context.user_data['sede_da_aggiungere'] = sede
        
        prefisso = get_prefisso_categoria(categoria)
        await query.edit_message_text(
            f"📝 **INSERISCI NUMERO**\n\n"
            f"**Articolo:** {CATEGORIE[categoria]}\n"
            f"**Sede:** {SEDI[sede]}\n"
            f"**Prefisso automatico:** `{prefisso}`\n\n"
            f"**Inserisci solo i numeri:**\n"
            f"Esempio: 001, 123, 456\n\n"
            f"Il codice completo sarà: `{prefisso}_NUMERO_{sede.upper()}`"
        )

    # RIMOZIONE ARTICOLO - SELEZIONE CATEGORIA
    elif data.startswith("rimuovi_cat_"):
        categoria = data[12:]
        articoli = get_articoli_per_stato('disponibile') + get_articoli_per_stato('usato') + get_articoli_per_stato('fuori_uso')
        articoli_categoria = [a for a in articoli if a[1] == categoria]
        
        if not articoli_categoria:
            await query.edit_message_text(f"❌ **Nessun articolo trovato per {CATEGORIE[categoria]}**")
            return
        
        keyboard = []
        for seriale, cat, sede in articoli_categoria:
            articolo_info = get_articolo(seriale)
            stato = articolo_info[4] if articolo_info else "sconosciuto"
            emoji_stato = "🟢" if stato == "disponibile" else "🔴" if stato == "usato" else "⚫"
            nome = f"{emoji_stato} {seriale} - {SEDI[sede]}"
            keyboard.append([InlineKeyboardButton(nome, callback_data=f"elimina_{seriale}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"➖ **ELIMINA ARTICOLO**\n\n"
            f"Categoria: {CATEGORIE[categoria]}\n"
            f"Seleziona l'articolo da rimuovere:",
            reply_markup=reply_markup
        )

    # RIMOZIONE ARTICOLO - CONFERMA ELIMINAZIONE
    elif data.startswith("elimina_"):
        seriale = data[8:]
        articolo = get_articolo(seriale)
        
        if articolo:
            delete_articolo(seriale)
            await query.edit_message_text(f"✅ **{seriale} rimosso dall'inventario!**")
        else:
            await query.edit_message_text(f"❌ **{seriale} non trovato!**")

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
    sede_testo = f" {SEDI[sede]}" if sede else ""
    
    if count <= SOGLIE_BOMBOLE["sotto_scorta"]:
        messaggio = f"🚨 **SOTTO SCORTA BOMBOLE{sede_testo}!**\nSolo {count} bombole disponibili!"
    elif count == SOGLIE_BOMBOLE["allarme_scorta"]:
        messaggio = f"🟡 **ALLARME SCORTA BOMBOLE{sede_testo}**\nSolo {count} bombole disponibili!"
    elif count == SOGLIE_BOMBOLE["preallarme"]:
        messaggio = f"🔶 **PREALLARME SCORTA BOMBOLE{sede_testo}**\nSolo {count} bombole disponibili!"

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
    return "🤖 Bot Telegram Autoprotettori Erba - ONLINE 🟢"

@app.route('/health')
def health():
    return "OK"

@app.route('/status')
def status():
    articoli = len(get_tutti_articoli())
    bombole = conta_bombole_disponibili()
    return f"Bot Active | Articoli: {articoli} | Bombole: {bombole}"

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

    print("🤖 Bot Autoprotettori Erba Avviato!")
    print("📍 Server: Render.com")
    print("🟢 Status: ONLINE")
    print("💾 Database: SQLite3")
    print("👥 Admin configurati:", len(ADMIN_IDS))
    application.run_polling()

if __name__ == '__main__':
    main()
