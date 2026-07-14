# Agente ConvAI "Tropicoqueta" — versionato qui

Il prompt dell'agente viveva **solo su ElevenLabs**: nessun diff, nessuna review,
nessun rollback. Una modifica sbagliata si scopriva in produzione, al telefono,
e non si sapeva cosa c'era prima. Da qui in poi la fonte di verità è questa
cartella; ElevenLabs è una copia deployata.

## File

| File | Cos'è |
|---|---|
| `prompt.md` | Il system prompt. È **il** file da modificare. |
| `agent.json` | Le manopole LLM (`llm`, `temperature`) + i campi che vale la pena rileggere in una review (first_message, dynamic variables, elenco tool). |
| `config.snapshot.json` | La config live completa. Non si modifica a mano: serve al diff e al rollback. |
| `tools/*.json` | I 13 server tool, uno per file. Sola lettura (si modificano dal pannello EL). |
| `backups/` | Snapshot automatico pre-`push`. |
| `tests/agent-tests.mjs` | Suite adversarial nativa (Agent Tests). |

## Flusso

```bash
python scripts/agent_sync.py pull        # ElevenLabs -> repo (allinea prima di lavorare)
python scripts/agent_sync.py diff        # qualcuno ha toccato l'agente dal pannello?
# ... modifica agent/prompt.md ...
python scripts/agent_sync.py push --yes  # repo -> ElevenLabs (fa il backup e verifica dopo il PATCH)
node agent/tests/agent-tests.mjs --repeat 3   # gate: il punteggio, non l'impressione
git commit                                    # il prompt entra nella storia col suo punteggio
```

`push` tocca l'agente **live di un cliente vero**: senza `--yes` si rifiuta, fa
sempre un backup in `backups/` prima del PATCH, e rilegge il prompt dopo per
verificare che sia andato davvero.

## Cosa NON tocca lo script

`push` invia solo `prompt`, `llm`, `temperature`. Voce, turn-taking, criteri di
valutazione, webhook e tool restano configurati sulla piattaforma: sono campi
che la piattaforma garantisce e che non ha senso far riscrivere a uno script
ogni volta. Il `pull` li registra comunque in `config.snapshot.json`, così una
modifica fatta dal pannello si vede nel diff.

## Test — leggere prima di lanciarli

L'agente ha server tool che **scrivono davvero** (`create_booking`,
`cancel_booking`, `notify_secretary`): una simulazione li eseguirebbe sul DB
della scuola. Ogni scenario della suite resta su percorsi di **sola lettura** e
il persona simulato ha l'ordine esplicito di non prenotare. Non aggiungere casi
di prenotazione senza un agente-copia.

Costo misurato: **~320 caratteri per simulazione** (workspace ElevenLabs
condiviso con altri clienti).
