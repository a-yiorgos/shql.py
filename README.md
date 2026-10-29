# SHQL.py - Flat-File SQL Interpreter in Python

`shql.py` is a single-file, self-contained Python reimplementation of Bruce Momjian's legacy `shql-1.3` flat-file SQL interpreter. It has **zero external dependencies** and uses standard `.csv` files for database table storage.

## Features

- **Single-File Architecture**: Entire interpreter (parser, REPL, execution engine, CSV storage manager, and test runner) is self-contained in `shql.py`.
- **CSV Storage Engine**: Uses Python's standard `csv` module to store data rows in `.csv` files and schema definitions in `.meta` files inside database directories.
- **Interactive REPL & Slash Commands**: Full support for legacy `shql` workspace slash commands.
- **Full SQL Subset**:
  - DDL: `CREATE TABLE`, `CREATE VIEW`, `DROP TABLE`, `DROP VIEW`
  - DML: `INSERT INTO` (values and subselects), `UPDATE ... SET ... WHERE`, `DELETE FROM ... WHERE`
  - Queries: `SELECT` (`DISTINCT`, multi-table joins, views, `WHERE` with `AND`/`OR`/`NOT` and subqueries `IN`/`NOT IN`, `ORDER BY ... NUM`, `UNION`, and aggregates `SUM`, `AVG`, `COUNT`, `MIN`, `MAX`)
  - Utility: `HELP` (`ALL`, `TABLES`, `VIEWS`, `COMMANDS`, table schemas, syntax help), `PRINT table`

## Usage

### Quick Start

```bash
# Create or target a database directory
mkdir -p /tmp/mydb

# Run shql interactively
python3 shql.py /tmp/mydb

# Run in quiet mode (no headers)
python3 shql.py -q /tmp/mydb

# Run demo queries
python3 shql.py /tmp/mydb < demo.shql

# Run built-in unit test suite
python3 shql.py --test
```

### Interactive Slash Commands

- `/g` - Execute SQL command in buffer
- `/p` - Print current SQL buffer
- `/p/g` (or `/pg`) - Print SQL buffer and execute
- `/r` - Reset/clear SQL buffer
- `/s` - Launch system shell (`$SHELL` or `/bin/sh`)
- `/y` (or `/py`) - Launch interactive Python shell (`code.interact`)
- `/e` - Open editor (`$EDITOR` if set, or prompt for buffer input)
- `/i` - Include file into buffer
- `/w` - Write buffer to output file
- `/q` - Quit interpreter
- `#` - Comments

## Differences & Incompatibilities with Original `shql-1.3`

1. **Storage Format**:
   - Original `shql-1.3` used tab-delimited flat files (`tablename~` for data and `tablename@` for metadata).
   - `shql.py` uses standard CSV files (`tablename.csv` for data and `tablename.meta` for metadata).
2. **Execution Engine & Shell Independence**:
   - Original `shql-1.3` relied on UNIX CLI tools (`awk`, `sed`, `grep`, `cut`, `sort`, `join`).
   - `shql.py` executes all SQL queries and condition evaluations in pure Python without forking external shell processes or UNIX utilities for SQL processing.
3. **Interactive Python Shell (`/y`)**:
   - `shql.py` adds `/y` (or `/py`) to launch an embedded Python interactive session (`code.interact()`) with direct access to database (`db`) and engine (`engine`) objects.
4. **Editor Fallback (`/e`)**:
   - If `$EDITOR` is set, `/e` opens `$EDITOR` with a temporary file. If `$EDITOR` is unset, `/e` falls back to reading input lines directly from stdin until EOF/Ctrl+D.
5. **Backticks Execution**:
   - Standard backtick commands like `` `date` `` or `` `ls / | wc -l` `` are evaluated natively in Python (`datetime`, directory file listing) without requiring external bash execution for SQL query parameters.
