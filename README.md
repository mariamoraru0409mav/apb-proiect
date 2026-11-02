# Proiect B: Integrarea angajatilor noi

Automatizarea proceseaza cererile de onboarding si transfer intern din hub-ul BPA, configureaza sistemele Directory, Access, LMS si Calendar, inregistreaza rezultatul, trimite notificarile si publica raportul final. Transferurile sunt acceptate numai cand `features.transfers` este activ in configuratia hub-ului.

## Cerinte

- Python 3.10 sau mai nou pentru `bpa_runner.py`.
- Solutia foloseste doar biblioteca standard Python; nu necesita instalarea unor pachete.
- Pentru programarea intalnirilor este necesara baza de fusuri orare pentru `Europe/Bucharest`.

## Configurare si rulare

Seteaza `BPA_HUB` la `https://apb.majordom.tech` si furnizeaza cheia personala prin variabila de mediu `BPA_KEY`. Nu introduce cheia in cod, README sau Git. Runner-ul nu transmite cheia procesului solutiei.

Din acest folder, ruleaza suita publica astfel:

```bash
python3 bpa_runner.py --project B --suite public -- python3 solutie.py {base_url}
```

Runner-ul creeaza rularea si inlocuieste `{base_url}` cu adresa ei. Solutia accepta adresa si ca prim argument sau prin `BPA_BASE_URL`.

Pentru a rula un set ales de scenarii, adauga, de exemplu, `--scenarios B-P01,B-P03` inainte de `--`. Pentru detalii despre reporniri, vezi optiunile `--kill-after-writes` si `--invocations` din `bpa_runner.py`.

## Implementare

`solutie.py` citeste contextul, configuratia si datele de referinta din hub. Proceseaza cererile in ordinea primita, normalizeaza datele, aplica regulile proiectului B si reutilizeaza starile deja inregistrate pentru a evita scrieri duplicate la reluare. Scrierile folosesc chei de idempotenta stabile; erorile temporare sunt reincercate conform ghidului.

## Cum se ruleaza la notare

```text
image: python:3.12-slim
setup: pip install -r requirements.txt
command: python solutie.py {base_url}
```
