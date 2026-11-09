# Atualização de instalações antigas

Instalações atuais guardam a sessão criptografada, os jobs, o estado da fila e os registros de mídias no SQLite configurado por `DB_PATH`. Preserve esse banco, o volume de downloads e a chave `TBMD_SESSION_KEY` ao atualizar.

## Histórico web anterior ao SQLite

Esta etapa se aplica apenas a versões antigas que ainda gravavam `jobs_history.json`. Instalações novas não precisam criar esse arquivo.

A importação ocorre uma única vez, quando o banco ainda não contém estado da fila web. A aplicação procura o arquivo ao lado do banco e depois na raiz do projeto. Um banco já inicializado tem prioridade, inclusive quando o histórico está vazio. Um JSON inválido impede a inicialização para evitar perda silenciosa; preserve e corrija a origem antes de tentar novamente.

### Docker

Aguarde o download ativo terminar. Antes de recriar o container antigo, copie seu histórico para o volume persistente:

```bash
docker compose exec tbmd-web python -c 'import shutil; shutil.copy2("/app/jobs_history.json", "/app/data/jobs_history.json")'
docker compose up -d --build
```

Ajuste o destino da cópia se você personalizou o diretório de `DB_PATH`. Se a versão antiga nunca criou o JSON, não há histórico web a importar; inicie normalmente.

Confira os jobs, sua ordem e o estado de pausa no painel. Depois de validar a importação, remova a origem do volume e confirme a restauração sem ela:

```bash
docker compose exec tbmd-web python -c 'from pathlib import Path; Path("/app/data/jobs_history.json").unlink()'
docker compose up -d --force-recreate
```

Faça essa verificação sem downloads ativos. O banco e os arquivos de mídia permanecem nos volumes. Não use `docker compose down -v`, pois ele remove o volume do banco.

### Execução fora do Docker

Inicie com o JSON antigo presente e o `DB_PATH` correto. Confira a importação, remova o JSON e reinicie para validar a restauração pelo banco. Históricos do host e do container devem ser importados nos respectivos bancos; não sobrescreva um com o outro.

## Comportamento após reiniciar

Jobs que aguardavam execução continuam na fila. A pausa e o contador de IDs são preservados. Jobs interrompidos durante a execução ficam cancelados e podem ser reenfileirados pelo painel. A migração não substitui a sessão criptografada nem os registros das mídias.
