# Permissions

`pco verify --path PCO_MEMORY`, `pco search`, and `pco history` are read
commands and need no write grant. Do not request a one-time write grant to
read or alter `PCO_MEMORY`; permanent workspace write permission is unsupported.
