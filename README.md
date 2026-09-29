# anki-korean-sync

Copies AnkiDroid's collection off the phone over adb (read-only), reads the
review log for the Korean decks, and replaces the content of the Google Doc
"Anki Korean study words" with the cards answered Again/Hard and the cards
first studied in the last 14 days. A private claude.ai project with that Doc
in its knowledge sees the current list. Standard-library Python only.

    ./anki-korean-sync decks            # list decks
    ./anki-korean-sync run [--dry-run]  # copy + write the Doc
    ./anki-korean-sync watch --every 15 # rerun whenever the collection changed

Config: `~/.config/anki-korean-sync/config.json`, e.g.

    {"decks": ["Stan's Korean", "Korean Vocab::Auction Industry", "HTSK Sentences"],
     "days": 14, "serial": "127.0.0.1:5555"}

Needs `adb` connected to the phone (`adb tcpip 5555` once per boot from any
adb connection, then `adb connect 127.0.0.1:5555` from Termux) and an rclone
Drive remote (`ANKI_KOREAN_DRIVE_REMOTE`, default `moneo`, scope drive.file).
The Doc id is kept in `~/.config/anki-korean-sync/doc-id`.
