# Interactions API

Stability: **stable**.

## Methods

`list_interactions`, `get_interaction`, `claim_interaction`, `release_interaction`,
`respond_to_interaction`, `send_interaction_secret` — see [methods.md](methods.md).

## Types

* `HOST_KEY_CONFIRMATION` — accept / reject
* `PASSWORD` / `PRIVATE_KEY_PASSPHRASE` — submit secret or cancel

## Password prompt presentation

Ordinary SSH `PasswordPrompt` values carry a safe `username` and `hostname`, a
null `secret_prompt_kind`, and an empty `secret_prompt_parameters` object.

Protected prompts owned by `SecretBackendService` instead carry empty
`username`/`hostname` fields, a stable `SecretPromptKind`, and exactly the
validated non-secret parameters allowed for that kind. The daemon does not
send a rendered English heading or body and does not select an interface
language. GTK maps the kind to gettext msgids, translates when presenting the
dialog, then applies the parameters. Email addresses, client IDs, backend
names, and other dynamic values remain data and are never msgids.

## Eligibility

Only the session/SFTP/forward **originating client** (or attachments) may list/claim/respond.
A second client_id cannot answer another client's prompts.

## State machine

`pending` → `claimed` → `answered` | `cancelled` | `expired` | `failed`

## Timeouts

* Secrets: 120s default
* Host-key: 180s default
* Expired interactions yield askpass decline

## Cancellation

`SecretDecision.CANCEL` finishes the interaction as `cancelled`; askpass returns
non-success; OpenSSH child exits; session auth-gate fails with `OPERATION_CANCELLED`
when applicable. Late responses after final state are rejected.

## Examples

```python
for item in client.list_interactions():
    if item.state is InteractionState.PENDING and item.type is InteractionType.HOST_KEY_CONFIRMATION:
        claim = client.claim_interaction(item.id)
        client.respond_to_interaction(InteractionDecisionRequest(
            interaction_id=item.id,
            host_key_decision=HostKeyDecision.ACCEPT,
        ))
```
