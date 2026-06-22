# Feishu Card Bot

This project can send Codex Flow review summaries as Feishu interactive cards and receive card button callbacks.

## Required Feishu App Settings

Use the app configured in `flow_config.yaml`:

- App ID: `cli_aa94ca5f7efa5cef`
- Test chat: `oc_f68aa0f27b4e5aa077b962a510f8ae4f`

Enable these permissions in the Feishu Developer Console:

- `im:message`
- `im:chat`
- `im:chat.members:write_only` if you want the API to invite the bot into a chat

Configure card callback / event request URL:

```text
https://<public-host>/feishu/card
```

Set the same Verification Token in Feishu and locally:

```powershell
$env:FEISHU_VERIFICATION_TOKEN = "<verification-token>"
$env:FEISHU_APP_SECRET = "<app-secret>"
```

## Run Callback Server

```powershell
python lark_card_bot.py --host 0.0.0.0 --port 8787
```

For local testing with Feishu, expose this port through a tunnel and use the public HTTPS URL in the Feishu console.

## Send Test Card

```powershell
python lark_card_bot.py --send-test-card
```

Button clicks are appended to:

```text
runs/feishu_card_actions.jsonl
```

Each line has the parsed command shape:

```json
{"action":"keep","supplement":null,"trial_id":"trial_034","card_action":true}
```

`/revise <suggestion>` and `/branch A;B` are mapped to `rollback` with a supplement, so the current Codex Flow loop can consume them without a new decision state.
