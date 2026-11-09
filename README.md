# Valocheck

Stima la probabilità che i giocatori delle tue partite di VALORANT stiano barando, e raccoglie i dati per verificare e migliorare quella stima.

- **In partita:** confronta ogni giocatore con gli altri delle sue ultime partite su headshot %, ADR, primi duelli, precisione angolare della mira al momento della kill e kill a raffica (meno di 300 ms tra due kill), più il profilo dell'account. Combina tutto in una stima bayesiana e mostra il risultato in una pagina del browser.
- **Ricerca:** salva i **rimborsi di RR** che Riot dà dopo aver bannato un cheater (campo ufficiale `RankedRatingRefundApplied`). Incrociando i rimborsi ricevuti da persone diverse si individuano **cheater confermati**, con cui calibrare il modello.

> ⚠️ Il risultato è una **stima statistica**, non una prova. I parametri del modello sono ipotesi iniziali finché non vengono calibrati sui dati raccolti.

Non fa instalock, non rivela il nome di chi gioca in modalità nascosta, non tocca i file del gioco e non inietta nulla: legge solo le informazioni che il client di VALORANT espone già sul tuo PC. Non è affiliato a Riot Games. Usalo a tuo rischio.

---

## Installazione (5 minuti, una volta sola)

### 1. Scarica Valocheck
1. In questa pagina clicca il pulsante verde **`<> Code`** → **Download ZIP**.
2. Apri la cartella **Download**, clicca col tasto destro sul file ZIP → **Estrai tutto…** → **Estrai**.

### 2. Installa Python (se non ce l'hai)
1. Premi **Windows + R**, scrivi `cmd`, premi **INVIO**.
2. Scrivi `py --version` e premi **INVIO**.
   - Se compare *Python 3.x.x*, ce l'hai già: passa al punto 3.
   - Altrimenti vai su <https://www.python.org/downloads/>, scarica e apri l'installatore. Se ti fa domande nella finestra nera: alla domanda sui *percorsi lunghi* (long paths) rispondi `n`, a quelle su *installare Python* o sul *PATH* rispondi `y`.

### 3. Prova che funzioni
Nella cartella estratta fai doppio clic su **STRUMENTI_RICERCA.bat**, scrivi `6` e premi **INVIO**. Deve comparire **"RISULTATO: tutte le verifiche superate"**.

> Se Windows mostra "Windows ha protetto il PC": clicca **Ulteriori informazioni** → **Esegui comunque**.

---

## Uso

1. Apri VALORANT e arriva al menu principale.
2. Doppio clic su **AVVIA_VALOCHECK.bat**. Si apre il browser con i risultati: lascia aperta la finestra nera.
3. Entra in coda. In selezione agente vedi la tua squadra; gli avversari compaiono appena la partita carica.
4. Clicca un giocatore per vedere da cosa dipende il suo numero.

---

## Raccogliere i dati insieme (Google Drive)

Più persone usano Valocheck, più rimborsi si raccolgono e prima si trovano i cheater confermati. Ognuno scrive **il proprio file** in una cartella condivisa e legge quelli degli altri. Nei file condivisi **non ci sono nomi**: solo codici anonimi dei giocatori, rimborsi, composizione delle squadre e statistiche.

**Chi crea la cartella (una volta):**
1. Vai su <https://drive.google.com> → **Nuovo** → **Nuova cartella** → chiamala esattamente **`Valocheck_condiviso`**.
2. Tasto destro sulla cartella → **Condividi** → inserisci la Gmail dell'amico, ruolo **Editor** → **Invia**.

**Chi riceve l'invito:**
1. Su <https://drive.google.com> → **Condivisi con me** → tasto destro su `Valocheck_condiviso` → **Organizza** → **Aggiungi scorciatoia** → **Il mio Drive**.

**Tutti:**
1. Installa **Google Drive per desktop**: <https://www.google.com/drive/download/>. Accedi con il tuo account Google.
2. In Esplora file compare un'unità **Google Drive (G:)**: dentro **Il mio Drive** deve esserci `Valocheck_condiviso`.
3. Controlla con **STRUMENTI_RICERCA.bat** → `9`: deve dire **"Tutto a posto: la condivisione funziona."**

Da quel momento Valocheck sincronizza da solo ogni 5 minuti mentre è aperto. Se la cartella si trova altrove, scrivi il percorso completo nel file `cartella_condivisa.txt`.

---

## Strumenti di ricerca (`STRUMENTI_RICERCA.bat`)

| Opzione | Cosa fa |
|---|---|
| 1 | Fase 0: verifica di `refunded_rr` con l'API Henrik (già conclusa, serve una chiave Henrik) |
| 2 | Fase 1: triangola i cheater confermati dai rimborsi |
| 3 | Fase 2: ricontrolla chi è ancora attivo dopo 60 giorni (candidati "onesti") |
| 4 | Esporta il dataset in CSV (per la Fase 4) |
| 5 | Demo con una lobby simulata (non serve VALORANT) |
| 6 | Test automatico dei calcoli |
| 7 | Diagnosi del collegamento ai server Riot |
| 8 | Fase 0b: rimborsi visti nei dati ufficiali Riot |
| 9 | Sincronizza adesso con la cartella condivisa |

## Risultati finora

- **Fase 0:** i rimborsi anti-cheat sono visibili nel campo ufficiale `RankedRatingRefundApplied` dello storico competitivo Riot. Su 932 partite di 53 giocatori: 6 rimborsi, coincidenti 6 su 6 con l'API Henrik. Il rimborso viene registrato sulla partita giocata **dopo** averlo ricevuto.

## File

- `valocheck.py`: il programma (un solo file, solo libreria standard di Python).
- `parametri.json`: si crea al primo avvio e contiene i numeri del modello, modificabili.
- `valocheck_dati\`: database locale, partite scaricate e rapporti. Resta sul tuo PC e non va caricato su GitHub.

La parte di connessione al client deriva da *Valorant-Tracker* di Riccardo030607 (licenza MIT, vedi `LICENSE`).
