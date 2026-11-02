# Arhitectura solutiei BPA B

## Scop si flux

`bpa_runner.py` este launcher-ul pus la dispozitie de curs: creeaza rularea pe hub, transmite solutiei `BASE_URL`, o porneste si afiseaza raportul. `solutie.py` contine automatizarea propriu-zisa si foloseste numai biblioteca standard Python. Cheia echipei ramane la runner; solutia nu primeste si nu trimite chei de autentificare.

La pornire, `Onboarding` citeste o singura data contextul, configuratia si datele de referinta. `main` preia lista ordonata din `GET /requests`, proceseaza fiecare cerere pana la capat, apoi trimite un singur `POST /report`. Pentru fiecare cerere, `process` consulta mai intai istoricul, decide daca trebuie reluata, apoi valideaza si, daca este cazul, executa in ordine contul, accesul, instruirile si intalnirea. Rezultatul este inregistrat inaintea notificarilor.

```text
bpa_runner.py -> solutie.py -> BPA hub
                  |             |-- cereri si referinte
                  |             |-- Directory / Access / LMS / Calendar
                  |             |-- istoric, notificari si raport
                  |-- jurnal pe cerere si mesaje de retry
```

## Componente si reguli

- `Hub` centralizeaza GET/POST, codificarea JSON, timeout-urile, tratarea erorilor si politica de retry. Nu contine reguli de business.
- `normalize_document`, `parse_start_date` si `normalize_name` aplica B-R01 si normalizarile N1-N6: campuri obligatorii, tipuri, date ISO sau `DD.MM.YYYY`, e-mail, coduri si nume normalizate. `TRANSFER` este acceptat numai cand `features.transfers` este `true`; in rest, tipul este invalid.
- `valid_cnp` verifica structura, seria, judetul, numarul serial, data nasterii si cifra de control pentru B-R02. `age_at_start` calculeaza varsta implinita si trateaza aniversarea din 29 februarie pentru B-R03.
- `validation_codes` aplica B-R04-B-R09: existenta rolului/departamentului, data de inceput fata de `business_date`, departamentele permise, managerul activ si conflictele dintre pachete. Pentru `NEW_HIRE`, un CNP deja folosit produce `ALREADY_EMPLOYED`; pentru `TRANSFER`, B-R08 cere un cont existent si produce `NO_EXISTING_ACCOUNT` daca nu gaseste unul. `outcome_for_validation` aplica precedenta `REJECTED > MANUAL_REVIEW`; o cerere respinsa la validare nu produce scrieri de configurare.
- `account_step` implementeaza B-R10: la `TRANSFER` reutilizeaza primul cont gasit dupa CNP si nu creeaza alt cont. La `NEW_HIRE` reutilizeaza contul aceluiasi `request_id`, construieste username-ul transliterat si adauga sufix numeric daca acesta este deja folosit.
- `access_step` acorda pachetele rolului si departamentului in ordine, sarind peste granturile active existente. La `TRANSFER`, citeste granturile contului, revoca intai pachetele active din afara setului tinta, apoi acorda pachetele tinta inactive; un conflict SoD este decis la validare, inaintea oricarei modificari de acces. `training_step` combina instruirile obligatorii cu cele ale rolului si calculeaza termenele in zile lucratoare, fara sarbatorile citite din hub (B-R11). Pentru transfer, instruirile deja completate la sau dupa limita `start_date` minus `training_recency_months` luni calendaristice nu se repeta; configurarile vechi sau neterminate nu impiedica reinscrierea.
- `meeting_days` si `meeting_step` implementeaza B-R12: cauta zilele lucratoare din fereastra configurata, citeste ocuparea managerului, parcurge sloturile in ordine si foloseste fusul orar configurat. Interogarea ocupabilitatii este impartita in intervale de cel mult 62 de zile, limita endpoint-ului.
- `finish_record` si `send_notifications` aplica rezultatele B-R13 si regulile de notificare. La esec definitiv al crearii contului, pasii urmatori sunt `SKIPPED`; la esecul altui pas, restul pasilor continua. Un rezultat `NO_SLOT` adauga codul `MEETING_NOT_SCHEDULED`. Notificarile nu includ CNP si sunt trimise numai dupa inregistrare.
- `process` aplica B-R14 pentru porniri repetate. `report` construieste cate un element pentru fiecare cerere, cu rezultatul si codurile finale.

Valorile de business (data, praguri de varsta, program, durata, pasul si fereastra intalnirilor, destinatarii) provin din `GET /context` si `GET /config`; catalogul, managerii si sarbatorile provin din endpoint-urile de referinta. Solutia ignora campurile necunoscute si trimite corpurile POST cu numai campurile definite de API.

## Extensia CR-1: Transferuri interne

Extensia este activata numai de `features.transfers`. Un transfer pastreaza validarea comuna si programarea intalnirii, dar reutilizeaza contul existent. Pentru acces, starea relevanta este cea a granturilor `ACTIVE` citite dupa username; granturile `REVOKED` nu sunt considerate active. Revocarile folosesc `POST /access/revocations` si cheia `{request_id}:revoke:{package_code}`. Dupa o repornire, reconcilierea porneste din nou de la granturile active curente.

Pentru instruiri, `training_recency_months` se citeste din `GET /config.transfer`. Data limita scade luni calendaristice din data de inceput si limiteaza ziua la ultima zi a lunii rezultate. Numai instruirile `COMPLETED` cu `completed_on` la sau dupa aceasta limita sunt pastrate fara reinscriere; inscrierile vechi sau nefinalizate se refac pentru transferul curent.

## Retry si idempotenta

`Hub.call` foloseste timeout de 10 secunde. Pentru erori de retea si raspunsuri 500/502/503/504 face cel mult trei incercari in total, asteptand valoarea `Retry-After` sau o secunda. Pentru `429 RATE_LIMITED` asteapta la fel si reincerca fara sa consume una dintre cele trei incercari, cel mult de zece ori. `429 RUN_REQUEST_LIMIT` si celelalte erori 4xx opresc procesarea. Fiecare retry este scris in stderr.

Fiecare POST de business foloseste aceeasi cheie determinista la fiecare retry:

| Scriere      | Cheie                                                                        |
| ------------ | ---------------------------------------------------------------------------- |
| Cont         | `{request_id}:account`                                                       |
| Grant        | `{request_id}:grant:{package_code}`                                          |
| Inscriere    | `{request_id}:training:{training_code}`                                      |
| Intalnire    | `{request_id}:meeting`                                                       |
| Inregistrare | `{request_id}:record`                                                        |
| Notificare   | `{request_id}:notify:{recipient_role}:{template}`                            |
| Raport       | `report`, apoi `report-2`, `report-3` etc. numai la `IDEMPOTENCY_KEY_REUSED` |

Dupa un conflict de cheie, solutia reciteste starea relevanta din hub si continua numai daca scrierea este prezenta. Granturile, inscrierile si intalnirile existente se refolosesc; notificarea se compara dupa perechea `recipient_role`/`template`. Cheile si corpurile stabile previn dublurile in cazul in care hub-ul a inregistrat scrierea, dar raspunsul s-a pierdut.

## Reporniri, iesiri si log

Nu se pastreaza progres critic in fisiere locale. La fiecare pornire, pentru fiecare cerere se citeste `GET /history`:

- Ultima inregistrare `THIS_RUN`: nu se repeta validarea, configurarea sau inregistrarea; se retrimit doar notificarile lipsa.
- Ultima inregistrare `EARLIER_RUN` cu rezultat `COMPLETED`, `REJECTED` sau `MANUAL_REVIEW`: cererea este raportata fara alte scrieri.
- Fara inregistrare anterioara sau cu `EARLIER_RUN` partial: procesarea continua, reutilizand obiectele deja create pentru acelasi `request_id`.

Dupa ultima cerere, raportul contine rezultatele finale pentru toate cererile, inclusiv cele deja procesate. Se scrie o linie per cerere in stderr (`request_id | rezultat | coduri`) si o linie pentru fiecare retry. Erorile care nu sunt esecuri definitive de configurare opresc programul cu un cod diferit de zero; runner-ul finalizeaza rularea si afiseaza raportul.

## Rulare

Este necesar Python 3.10 sau mai nou pentru runner. Solutia nu necesita pachete instalate, dar runtime-ul trebuie sa aiba datele de fus orar pentru `Europe/Bucharest` disponibile pentru `zoneinfo`. Cheia `BPA_KEY` se configureaza in mediul runner-ului si nu se salveaza in cod, in log sau in repository.
