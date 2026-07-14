# Sample voce — Tropicoqueta

Stessa identica frase, stesse impostazioni. Cambia solo la variabile in esame.

Ogni sample esiste in due versioni:
- `*_hd.mp3` — **44,1 kHz**: è quello che senti nel widget del browser.
- `*_tel.wav` — **8 kHz**: è quello che esce **davvero dal telefono**.

> ⚠️ Ascolta prima una coppia `hd` / `tel` della stessa voce. La differenza che senti lì
> **non è colpa dell'agente**: la linea telefonica è a 8 kHz per costruzione (PSTN).
> Confrontare il widget del tutor (16 kHz) con Tropicoqueta al telefono (8 kHz) non è un confronto equo.

---

## 1. Quale voce — ascolta i `_hd.mp3`, poi ricontrolla il preferito in `_tel.wav`

| file | voce | etichette ElevenLabs |
|---|---|---|
| `0_ATTUALE_Carla_formale` | Carla Castro | `use_case: social_media` · `descriptive: **formal**` · fiorentino |
| `1_Ginevra_cheerful` | Ginevra | `conversational` · `confident` · giovane |
| `2_Rossana_warm` | Rossana | `conversational` · `casual` · standard |
| `3_Giusy_customer_care` | Giusy | `conversational` · `chill` · siciliano — voce tarata su customer service |
| `4_Aurora_milanese` | Aurora | `conversational` · `calm` · milanese |
| `5_Giulia_passionate` | Giulia | `conversational` · `excited` · giovane |

La voce attuale (`0`) è l'unica etichettata **`formal` / `social_media`**: è pensata per *leggere*, non per
*conversare*. È il motivo per cui suona statica, a parità di tutto il resto.

## 2. Quale modello TTS — il trade-off vero

| file | modello | espressività | velocità |
|---|---|---|---|
| `MODELLO_A_v3_espressivo` | `eleven_v3_conversational` (attuale) | alta (tag emotivi) | **`speed` non funziona** |
| `MODELLO_B_flash_veloce` | `eleven_flash_v2_5` (quello del tutor) | più piatta | `speed` funziona (qui a 1.10) |

Misurato, non ipotizzato — stessa frase:

| modello | speed 0.8 | speed 1.0 | speed 1.2 |
|---|---|---|---|
| `eleven_v3` | 4.80s | 4.80s | 4.48s |
| `eleven_flash_v2_5` | 5.67s | 4.46s | **3.72s** |

Su v3 il parametro `speed` è ignorato. Non si possono avere entrambe le cose: scegli quale ti pesa di più.
