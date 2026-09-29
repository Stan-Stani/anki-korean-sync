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

Needs `adb` to the phone and an rclone Drive remote (`ANKI_KOREAN_DRIVE_REMOTE`,
default `moneo`, scope drive.file). Running on the phone itself with serial
127.0.0.1:5555, it recovers after a reboot on its own if Wireless debugging
is on: the pairing survives reboots, so it finds the wireless-debugging port
among the phone's open local ports and reruns `adb tcpip 5555`. Elsewhere, run
`adb tcpip 5555` once per boot. The Doc id is kept in
`~/.config/anki-korean-sync/doc-id`.
