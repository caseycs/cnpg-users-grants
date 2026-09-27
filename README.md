# cnpg-users-grants

Keep PostgreSQL users and grants on [CloudNativePG](https://cloudnative-pg.io) clusters in plain YAML, and see what drifted.

## The problem

With a few CNPG clusters, roles and grants spread across ArgoCD values files, one-off SQL scripts and people's memory. After a while nobody can say who has access where, why a worker suddenly gets `permission denied`, or which grants a SQL file from last year actually left behind.

## What it's for

- **Snapshot** the users and grants of a live cluster into a reviewable YAML file.
- **Detect drift**: print the exact `GRANT`/`REVOKE` SQL that makes a cluster match its file. It exits non-zero on drift, so it works in CI.
- **Offboard people**: remove someone from the file, get a PR that marks their CNPG role `ensure: absent`.
- **Keep passwords in sync**: set each human's password from AWS SSM, without the plaintext appearing anywhere.

Nothing runs on a database by itself: grant SQL is printed for you to review and run. Only `sync-users --apply` changes things (a PR, then passwords).

## Quick start

Requires [uv](https://docs.astral.sh/uv/), a kubeconfig with access to the clusters, `gh` logged in (for `sync-users`) and AWS credentials (SSM, EKS).

```sh
mkdir cnpg-users-grants && cd cnpg-users-grants   # configs live in the directory you run from
cat > user_passwords_store.yaml <<'EOF'
aws_profile: default
aws_region: eu-central-1
aws_ssm_prefix: /cnpg-user/
EOF
mkdir dbs && cat > dbs/cloudnative-pg.my-app.prod.yaml <<'EOF'
context: my-kube-context
namespace: my-app
cluster: cloudnative-pg
repo: my-org/argocd
values_file: prod/my-app/cloudnative-pg/values.yaml
values_roles_path: roles           # where the CNPG roles list is in values_file (see below)
EOF

# fill humans/apps/grants from the live cluster
uvx --from git+https://github.com/caseycs/cnpg-users-grants cnpg-users import cloudnative-pg.my-app.prod --write
# anything drifted?
uvx --from git+https://github.com/caseycs/cnpg-users-grants cnpg-users sync-grants
```

`values_roles_path` is the dotted path to the list of [CNPG managed roles](https://cloudnative-pg.io/documentation/current/declarative_role_management/) inside `values_file`: `roles` when it's at the top level, `cluster.roles` when your chart nests it under `cluster:`. `sync-users` edits that list.

`uvx` fetches and caches the tool on first use; add `@<tag or commit>` to the URL to pin a version, or `uv tool install git+https://github.com/caseycs/cnpg-users-grants` to keep `cnpg-users` on your PATH.

## Commands

| Command | What it does |
|---|---|
| `import <db> [--write]` | Read roles and grants from the live cluster, show the diff against `dbs/<db>.yaml`; `--write` saves it. |
| `sync-grants [<db>…]` | Print the SQL that makes live grants match the file. |
| `sync-users [<db>…]` | Print the values.yaml change and password statements for humans. |
| `sync-users --apply` | Open one PR per repo, wait for it to be merged and synced, then set passwords. |

Without db names, `sync-*` run for every file in `dbs/`, `--parallel N` at a time (default 4). Exit codes: `0` in sync, `3` drift, `1` error, `2` usage.

## Files

`dbs/<db>.yaml`, one per database:

```yaml
context: my-kube-context           # kubeconfig context
namespace: my-app
cluster: cloudnative-pg            # CNPG Cluster name
repo: my-org/argocd                # where the CNPG values.yaml lives
values_file: prod/my-app/cloudnative-pg/values.yaml
values_roles_path: roles           # path to the CNPG roles list in values_file (default: roles)
online: true                       # false: skip this db
ignored_grantees: [pg_monitor]     # never touch these roles' grants
humans:
  - name: alice
    superuser: false
    roles: [pg_read_all_data]
apps: [webapp, workers]
grants:
  app_db:                          # database
    workers:                       # grantee
      - GRANT SELECT, INSERT ON TABLE public.events TO workers;
      - GRANT SELECT, USAGE ON SEQUENCE public.events_id_seq TO workers;
```

`user_passwords_store.yaml`: where human passwords live, one SSM parameter per role (`<aws_ssm_prefix><role>`).

## How roles are classified

`import` decides, per live role:

- **skipped**: `pg_*` / `cnpg_*` built-ins, CNPG-reserved roles, `ensure: absent`
- **app**: a `DatabaseRole` CR, or a password from a k8s secret
- **human**: a password parameter in SSM
- **app**: everything else

## Good to know

- `import` writes the shortest grant list that reproduces live grants exactly (`ON ALL TABLES IN SCHEMA`, one parent grant covering its partitions) and warns if it doesn't round-trip.
- `import --write` rebuilds `grants:` and `humans:` from the live cluster, so apply your pending SQL or PR first, or your edits are lost.
- `--apply` reuses an open PR labeled `cnpg-users-grants` (rewriting its branch) instead of opening a new one.
- Password statements carry a SCRAM verifier computed locally, never the plaintext. Missing SSM passwords are generated on `--apply`.
- EKS logins are signed in-process with boto3; other kubeconfig auth works as usual.

## Development

```sh
uv run pytest                    # tests
uv run cnpg-users sync-grants    # run from the checkout (configs from the current directory)
```
