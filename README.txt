GGE FELSZERELÉSKEZELŐ - GUI FEJLESZTŐI VERZIÓ
=============================================

Indítás:
  py gui.py

Szükséges:
  Python 3.10+
  pip install -r requirements.txt

Fájlok:
  gui.py             - grafikus felület
  equipment_core.py  - folyamatos WebSocket kapcsolat + felszerelés műveletek
  state.py           - parancsnok/felszerelés állapot
  protocol.py        - %xt% protokoll
  err.json           - hibakódok
  config.json        - opcionális, a GUI betölti és a "Beállítások mentése" gomb írja

Működés:
  1. Add meg az account adatait.
  2. Kattints a Kapcsolódás gombra.
  3. A program egyszer jelentkezik be és végig ugyanabban a sessionben marad.
  4. Ezután a gombokkal használható:
       - Felszerelés mentése
       - Felszerelések levétele
       - Felszerelések visszaállítása
       - Állapot frissítése
  5. Egyik felszerelésművelet sem indul el automatikusan.

Mentések:
  commander_loadouts/player_<PLAYER_ID>.json

Biztonsági viselkedés:
  - meglévő snapshot felülírásához külön megerősítés kell;
  - levétel csak olyan LID-kre történik, amelyekhez a snapshotban korábban volt felszerelés;
  - mozgásban lévő parancsnokokat kihagyja;
  - restore VIS-hely alapján történik;
  - foglalt slotot nem ír felül;
  - másik parancsnokon lévő mentett EID-t nem mozgat át automatikusan.

Megjegyzés:
  A config.json a jelszót jelenleg egyszerű szövegként tárolja, ugyanúgy, mint a
  korábbi konzolos verzió. Ha később szövetségi terjesztésre készül az .exe,
  ezt érdemes biztonságosabb hitelesítési tárolásra cserélni.
