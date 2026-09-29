# Changelog

## [1.0.0](https://github.com/caseycs/cnpg-users-grants/compare/v0.2.0...v1.0.0) (2026-09-27)


### ⚠ BREAKING CHANGES

* passwords_store.yaml instead of user_passwords_store.yaml ([#13](https://github.com/caseycs/cnpg-users-grants/issues/13))
* clusters/ instead of dbs/, MIT license, README leads with grants ([#9](https://github.com/caseycs/cnpg-users-grants/issues/9))

### Features

* clusters/ instead of dbs/, MIT license, README leads with grants ([#9](https://github.com/caseycs/cnpg-users-grants/issues/9)) ([9972a77](https://github.com/caseycs/cnpg-users-grants/commit/9972a77750a6903802e32b885501b1adf7ac44bf))
* GitLab repos for sync-users (merge requests via glab) ([#11](https://github.com/caseycs/cnpg-users-grants/issues/11)) ([5dd9ec1](https://github.com/caseycs/cnpg-users-grants/commit/5dd9ec1ee38b4560dbc3ac860aa8ecf45cb17547))
* passwords_store.yaml instead of user_passwords_store.yaml ([#13](https://github.com/caseycs/cnpg-users-grants/issues/13)) ([aeb49c2](https://github.com/caseycs/cnpg-users-grants/commit/aeb49c2fb3fb36dd3ede08e43684a42cf40fe336))


### Bug Fixes

* send SQL to psql over stdin, not the exec command line ([#8](https://github.com/caseycs/cnpg-users-grants/issues/8)) ([7f136ee](https://github.com/caseycs/cnpg-users-grants/commit/7f136eee80cf62889915b57e151fe1f62ff44f6a))

## [0.2.0](https://github.com/caseycs/cnpg-users-grants/compare/v0.1.0...v0.2.0) (2026-09-27)


### Features

* pluggable password stores (AWS SSM, GCP Secret Manager, sops) ([a12f0eb](https://github.com/caseycs/cnpg-users-grants/commit/a12f0eb8579370ece5be4313efb899a7bf6b4b51))
* pluggable password stores (AWS SSM, GCP Secret Manager, sops) ([8c6375e](https://github.com/caseycs/cnpg-users-grants/commit/8c6375e7d370dbc285e2f357a6dd273e0097bb7d))
* sync command for users and grants across all dbs at once ([1de78e3](https://github.com/caseycs/cnpg-users-grants/commit/1de78e3b92e97fe4aa38754eb8b97fbefa9c955c))
* sync command for users and grants across all dbs at once ([6c81c54](https://github.com/caseycs/cnpg-users-grants/commit/6c81c5476402e202935f6f4782e9ac0b810e4981))
