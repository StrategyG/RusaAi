# API Authentication and Credential Lifecycle

The API accepts tenant-bound credentials through the `X-API-Key` header. Production
must use `RUSA_API_KEYS_JSON`, a registry keyed by the lowercase SHA-256 digest
of each raw high-entropy API key. Do not store raw keys in source control,
container images, or ordinary configuration files.

## Principal registry

Each production entry must contain:

- `subject`: stable service or user identity (maximum 128 characters)
- `tenant_id`: tenant identity
- `roles`: one or more known API roles
- `not_before`: Unix timestamp from which the key is valid
- `expires_at`: Unix timestamp at which the key stops being valid
- `revoked`: optional boolean, defaults to `false`

Key lifetime is limited to 30 days. The API rejects keys that are not yet valid,
expired, revoked, malformed, or shorter than 32 UTF-8 bytes. Production startup
validation also requires at least one currently active, non-revoked principal.

Example shape (replace all values and timestamps):

```json
{
  "<64-character-lowercase-sha256-digest>": {
    "subject": "deployment-agent",
    "tenant_id": "tenant-a",
    "roles": ["developer"],
    "not_before": 1800000000,
    "expires_at": 1800086400,
    "revoked": false
  }
}
```

Generate a digest outside the repository:

```bash
python -c 'import hashlib, getpass; print(hashlib.sha256(getpass.getpass("API key: ").encode()).hexdigest())'
```

## Rotation and revocation

1. Generate a new random key with at least 32 bytes of entropy.
2. Add its SHA-256 digest and active principal claims to the secret-managed
   `RUSA_API_KEYS_JSON` registry.
3. Deploy/restart the API so the new registry is loaded.
4. Move clients to the new key.
5. Remove the old digest or set its `revoked` claim to `true`, then deploy
   again. The registry is loaded at process startup; changing the secret store
   alone does not update already-running processes.
6. Confirm the old key receives HTTP 401 and the new key is accepted.

Never extend a key beyond its 30-day lifetime. Use overlapping active keys
during rotation to avoid client downtime.

## Development compatibility

The raw `RUSA_API_KEYS` mapping is a development-only compatibility path and is
forbidden in production. The no-key development bypass is disabled by default.
It can be enabled only by setting
`RUSA_ALLOW_INSECURE_DEVELOPMENT_AUTH=true` in development, and it is accepted
only for loopback or test-client requests. It is never accepted in production.

Production API deployment must use `ENVIRONMENT=production`, an explicit
`RUSA_API_DATABASE_URL`, and the hashed principal registry. Tenant request
rate limits are applied after successful authentication; unauthenticated
traffic should additionally be limited at the ingress or API gateway.
