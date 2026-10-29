#!/usr/bin/env python3
"""
MIT License

Copyright (c) 2026 Yiorgos Adamopoulos

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

shql.py - A self-contained flat-file SQL interpreter in Python.
Replicates Bruce Momjian's shql-1.3 flat-file SQL interpreter with CSV storage.
Zero external dependencies.
"""

import sys
import os
import csv
import argparse
import pathlib
import re
import tempfile
import unittest
import io
import time
import datetime
import code
import subprocess

SHQL_ROOT = os.environ.get("SHQL_ROOT", "/u/shql")


class Database:
    """Manages table storage and view metadata in the database directory."""

    def __init__(self, db_path: str):
        self.db_dir = self.resolve_db_dir(db_path)
        if not os.path.exists(self.db_dir):
            raise FileNotFoundError(f"Unknown database ({db_path})")

    @staticmethod
    def resolve_db_dir(db_path: str) -> str:
        home = os.environ.get("HOME", "")
        candidates = [
            db_path,
            os.path.join(home, "shql", db_path) if home else None,
            os.path.join(SHQL_ROOT, db_path) if SHQL_ROOT else None,
            os.path.join(home, db_path) if home else None,
        ]
        for cand in candidates:
            if cand and os.path.isdir(cand):
                return os.path.abspath(cand)
        return os.path.abspath(db_path)

    def table_csv_path(self, table_name: str) -> str:
        return os.path.join(self.db_dir, f"{table_name.lower()}.csv")

    def table_meta_path(self, table_name: str) -> str:
        return os.path.join(self.db_dir, f"{table_name.lower()}.meta")

    def view_path(self, view_name: str) -> str:
        return os.path.join(self.db_dir, f"{view_name.lower()}.view")

    def table_exists(self, name: str) -> bool:
        name = name.lower()
        return os.path.exists(self.table_meta_path(name))

    def view_exists(self, name: str) -> bool:
        name = name.lower()
        return os.path.exists(self.view_path(name))

    def create_table(self, name: str, columns: list[tuple[str, int]]):
        name = name.lower()
        meta_p = self.table_meta_path(name)
        csv_p = self.table_csv_path(name)
        with open(meta_p, "w", newline="") as f:
            writer = csv.writer(f)
            for col_name, col_width in columns:
                writer.writerow([col_name, str(col_width)])
        if not os.path.exists(csv_p):
            open(csv_p, "w", newline="").close()

    def get_table_schema(self, name: str) -> list[tuple[str, int]]:
        name = name.lower()
        meta_p = self.table_meta_path(name)
        if not os.path.exists(meta_p):
            raise ValueError(f"{name} does not exist.")
        schema = []
        with open(meta_p, "r", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if row:
                    schema.append((row[0], int(row[1])))
        return schema

    def read_rows(self, name: str) -> list[list[str]]:
        name = name.lower()
        csv_p = self.table_csv_path(name)
        if not os.path.exists(csv_p):
            return []
        rows = []
        with open(csv_p, "r", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(row)
        return rows

    def write_rows(self, name: str, rows: list[list[str]]):
        name = name.lower()
        csv_p = self.table_csv_path(name)
        with open(csv_p, "w", newline="") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(row)

    def append_rows(self, name: str, rows: list[list[str]]):
        name = name.lower()
        csv_p = self.table_csv_path(name)
        with open(csv_p, "a", newline="") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(row)

    def create_view(self, name: str, t1: str, f1: str, t2: str, f2: str):
        name = name.lower()
        view_p = self.view_path(name)
        with open(view_p, "w") as f:
            f.write(f"{t1.lower()} {f1} {t2.lower()} {f2} {name.lower()}\n")

    def get_view_def(self, name: str) -> tuple[str, str, str, str, str] | None:
        name = name.lower()
        view_p = self.view_path(name)
        if not os.path.exists(view_p):
            return None
        with open(view_p, "r") as f:
            content = f.read().strip().split()
            if len(content) == 5:
                return tuple(content)
        return None

    def drop_table(self, name: str):
        name = name.lower()
        for p in [self.table_meta_path(name), self.table_csv_path(name)]:
            if os.path.exists(p):
                os.remove(p)

    def drop_view(self, name: str):
        name = name.lower()
        p = self.view_path(name)
        if os.path.exists(p):
            os.remove(p)


def eval_backticks(text: str) -> str:
    """
    Evaluates backtick expressions in pure Python.
    Handles 'date', 'ls ... | wc -l', or standard Python expressions.
    """

    def replacer(match):
        cmd = match.group(1).strip()
        cmd_l = cmd.lower()
        if cmd_l == "date":
            now = datetime.datetime.now(datetime.timezone.utc)
            return now.strftime("%a %b %d %H:%M:%S UTC %Y")
        elif "wc -l" in cmd_l and ("ls" in cmd_l or "dir" in cmd_l):
            m_path = re.search(r"ls\s+([^\|]+)", cmd, re.IGNORECASE)
            target = m_path.group(1).strip() if m_path else "."
            target = os.path.expanduser(target)
            try:
                if os.path.isdir(target):
                    count = len(os.listdir(target))
                else:
                    count = 1
                return str(count)
            except Exception:
                return "0"
        else:
            try:
                res = eval(cmd, {"__builtins__": {}}, {})
                return str(res)
            except Exception:
                return cmd

    return re.sub(r"`([^`]+)`", replacer, text)


def tokenize_sql(sql_str: str) -> list[str]:
    """
    Tokenizes SQL string matching shql-1.3 rules.
    Commas are treated as token separators.
    """
    tokens = []
    i = 0
    n = len(sql_str)
    while i < n:
        c = sql_str[i]
        if c.isspace() or c == ",":
            i += 1
            continue

        if c in ("'", '"'):
            quote = c
            i += 1
            val = ""
            while i < n and sql_str[i] != quote:
                if sql_str[i] == "\\" and i + 1 < n:
                    i += 1
                val += sql_str[i]
                i += 1
            if i < n and sql_str[i] == quote:
                i += 1
            tokens.append(f"'{val}'")
            continue

        if c in ("=", "!", ">", "<"):
            if i + 1 < n and sql_str[i + 1] == "=":
                tokens.append(sql_str[i : i + 2])
                i += 2
            elif c == "!" and i + 1 < n and sql_str[i + 1] == "=":
                tokens.append("!=")
                i += 2
            elif c in (">", "<") and i + 1 < n and sql_str[i + 1] in (">", "<", "="):
                tokens.append(sql_str[i : i + 2])
                i += 2
            else:
                tokens.append(c)
                i += 1
            continue

        if c in ("(", ")"):
            tokens.append(c)
            i += 1
            continue

        start = i
        while i < n and not sql_str[i].isspace() and sql_str[i] not in (",", "'", '"', "=", "!", ">", "<", "(", ")"):
            i += 1
        tokens.append(sql_str[start:i])

    return tokens


def strip_quotes(val: str) -> str:
    if len(val) >= 2 and ((val[0] == "'" and val[-1] == "'") or (val[0] == '"' and val[-1] == '"')):
        return val[1:-1]
    return val


def format_table_output(headers: list[tuple[str, int]], rows: list[list[str]], quiet: bool = False, is_agg: bool = False) -> str:
    out = []
    if not quiet or is_agg:
        hdr_line = ""
        for name, width in headers:
            text = f"-{name}-"
            fmt_spec = f"%-{width}.{width}s"
            hdr_line += f"|{fmt_spec % text}"
        hdr_line += "|"
        out.append(hdr_line)

    for row in rows:
        row_line = ""
        for i, (name, width) in enumerate(headers):
            val = row[i] if i < len(row) else ""
            fmt_spec = f"%-{width}.{width}s"
            row_line += f"|{fmt_spec % val}"
        row_line += "|"
        out.append(row_line)

    num_rows = len(rows)
    if is_agg:
        num_rows = 0
    out.append(f"( {num_rows} rows)")

    return "\n".join(out) + "\n"


def compare_vals(v1, op, v2) -> bool:
    try:
        f1 = float(v1)
        f2 = float(v2)
        if op in ("==", "="):
            return f1 == f2
        if op == "!=":
            return f1 != f2
        if op == ">":
            return f1 > f2
        if op == "<":
            return f1 < f2
        if op == ">=":
            return f1 >= f2
        if op == "<=":
            return f1 <= f2
    except (ValueError, TypeError):
        s1 = str(v1)
        s2 = str(v2)
        if op in ("==", "="):
            return s1 == s2
        if op == "!=":
            return s1 != s2
        if op == ">":
            return s1 > s2
        if op == "<":
            return s1 < s2
        if op == ">=":
            return s1 >= s2
        if op == "<=":
            return s1 <= s2
    return False


class QueryEngine:
    """Executes SQL statements on Database."""

    def __init__(self, db: Database, quiet: bool = False):
        self.db = db
        self.quiet = quiet

    def execute(self, tokens: list[str], output_stream):
        if not tokens:
            return

        cmd = tokens[0].lower()

        try:
            if cmd == "create":
                self.do_create(tokens, output_stream)
            elif cmd == "drop":
                self.do_drop(tokens, output_stream)
            elif cmd == "insert":
                self.do_insert(tokens, output_stream)
            elif cmd == "delete":
                self.do_delete(tokens, output_stream)
            elif cmd == "update":
                self.do_update(tokens, output_stream)
            elif cmd == "help":
                self.do_help(tokens, output_stream)
            elif cmd == "print":
                self.do_print(tokens, output_stream)
            elif cmd == "select":
                res = self.do_select(tokens)
                if res is not None:
                    output_stream.write(res)
            else:
                sys.stderr.write("Missing or unrecognized command.\n")
        except Exception as e:
            sys.stderr.write(f"Error: {e}\n")

    def do_create(self, tokens: list[str], output_stream):
        if len(tokens) < 3:
            sys.stderr.write("Improper syntax (create)\n")
            return

        target_type = tokens[1].lower()
        name = tokens[2].lower()

        if self.db.table_exists(name) or self.db.view_exists(name):
            sys.stderr.write("Table already exists.\n")
            return

        if target_type == "view":
            if len(tokens) < 7 or tokens[3] != "(" or tokens[5] != "=" or tokens[-1] != ")":
                sys.stderr.write("Bad syntax\n")
                return
            expr1 = tokens[4].split(".")
            expr2 = tokens[6].split(".")
            if len(expr1) != 2 or len(expr2) != 2:
                sys.stderr.write("Bad table or field name\n")
                return
            t1, f1 = expr1[0], expr1[1]
            t2, f2 = expr2[0], expr2[1]

            if not self.db.table_exists(t1) and not self.db.view_exists(t1):
                sys.stderr.write("Bad table or field name\n")
                return
            if not self.db.table_exists(t2) and not self.db.view_exists(t2):
                sys.stderr.write("Bad table or field name\n")
                return

            self.db.create_view(name, t1, f1, t2, f2)
            output_stream.write("OK\n")

        elif target_type == "table":
            if len(tokens) < 6 or tokens[3] != "(" or tokens[-1] != ")":
                sys.stderr.write("Improper syntax (create)\n")
                return
            col_tokens = tokens[4:-1]
            if len(col_tokens) % 2 != 0:
                sys.stderr.write("Bad syntax\n")
                return
            cols = []
            for i in range(0, len(col_tokens), 2):
                cname = col_tokens[i]
                try:
                    cwidth = int(col_tokens[i + 1])
                except ValueError:
                    sys.stderr.write("Bad syntax\n")
                    return
                cols.append((cname, cwidth))
            self.db.create_table(name, cols)
            output_stream.write("OK\n")
        else:
            sys.stderr.write(f"Improper syntax ({tokens[1]})\n")

    def do_drop(self, tokens: list[str], output_stream):
        if len(tokens) < 3:
            sys.stderr.write("Syntax error.\n")
            return
        target_type = tokens[1].lower()
        name = tokens[2].lower()

        if target_type not in ("table", "view"):
            sys.stderr.write("Syntax error.\n")
            return

        if target_type == "table" and self.db.view_exists(name):
            sys.stderr.write(f"Can not drop, {tokens[2]} is a view, not a table\n")
            return
        if target_type == "view" and not self.db.view_exists(name):
            sys.stderr.write(f"Can not drop, {tokens[2]} is not a view\n")
            return

        if self.db.table_exists(name) or self.db.view_exists(name):
            if target_type == "view":
                self.db.drop_view(name)
            else:
                self.db.drop_table(name)
            output_stream.write("OK\n")
        else:
            sys.stderr.write("No such table\n")

    def do_insert(self, tokens: list[str], output_stream):
        if len(tokens) < 3 or tokens[1].lower() != "into":
            sys.stderr.write("Improper syntax (insert)\n")
            return

        table_name = tokens[2].lower()
        if self.db.view_exists(table_name):
            sys.stderr.write("Can not insert into a view\n")
            return
        if not self.db.table_exists(table_name):
            sys.stderr.write("Table does not exist\n")
            return

        schema = self.db.get_table_schema(table_name)
        specified_cols = None

        idx = 3
        if idx < len(tokens) and tokens[idx] == "(":
            idx += 1
            specified_cols = []
            while idx < len(tokens) and tokens[idx] != ")":
                specified_cols.append(tokens[idx].lower())
                idx += 1
            if idx >= len(tokens) or tokens[idx] != ")":
                sys.stderr.write("Syntax error (insert)\n")
                return
            idx += 1

        if idx >= len(tokens):
            sys.stderr.write("Improper syntax (insert)\n")
            return

        next_word = tokens[idx].lower()
        rows_to_insert = []

        if next_word == "values":
            idx += 1
            if idx >= len(tokens) or tokens[idx] != "(" or tokens[-1] != ")":
                sys.stderr.write("Improper syntax (insert)\n")
                return
            val_tokens = tokens[idx + 1 : -1]

            expected_num = len(specified_cols) if specified_cols else len(schema)
            if expected_num == 0 or len(val_tokens) % expected_num != 0:
                sys.stderr.write("Incorrect number of values.\n")
                return

            num_rows = len(val_tokens) // expected_num
            for r in range(num_rows):
                chunk = [strip_quotes(v) for v in val_tokens[r * expected_num : (r + 1) * expected_num]]
                if specified_cols:
                    full_row = [""] * len(schema)
                    col_map = {name.lower(): i for i, (name, _) in enumerate(schema)}
                    for col_n, val_v in zip(specified_cols, chunk):
                        if col_n in col_map:
                            full_row[col_map[col_n]] = val_v
                        else:
                            sys.stderr.write(f"Bad field name. ({col_n})\n")
                            return
                    rows_to_insert.append(full_row)
                else:
                    rows_to_insert.append(chunk)

        elif next_word == "select":
            select_tokens = tokens[idx:]
            res_rows = self.eval_select_rows(select_tokens)
            if res_rows is None:
                return
            for r in res_rows:
                if specified_cols:
                    full_row = [""] * len(schema)
                    col_map = {name.lower(): i for i, (name, _) in enumerate(schema)}
                    for col_n, val_v in zip(specified_cols, r):
                        if col_n in col_map:
                            full_row[col_map[col_n]] = val_v
                    rows_to_insert.append(full_row)
                else:
                    rows_to_insert.append(r)
        else:
            sys.stderr.write(f"Improper syntax ({tokens[idx]})\n")
            return

        self.db.append_rows(table_name, rows_to_insert)
        output_stream.write(f"({len(rows_to_insert)} rows)\n")

    def do_delete(self, tokens: list[str], output_stream):
        if len(tokens) < 3 or tokens[1].lower() != "from":
            sys.stderr.write("Improper syntax (delete)\n")
            return

        table_name = tokens[2].lower()
        if self.db.view_exists(table_name):
            sys.stderr.write("You can not delete from a view.\n")
            return
        if not self.db.table_exists(table_name):
            sys.stderr.write(f"{tokens[2]} does not exist.\n")
            return

        all_rows = self.db.read_rows(table_name)
        schema = self.db.get_table_schema(table_name)

        if len(tokens) > 3 and tokens[3].lower() == "where":
            where_tokens = tokens[4:]
            kept_rows = []
            deleted_count = 0
            for row in all_rows:
                if self.eval_where_condition(where_tokens, schema, row, table_name):
                    deleted_count += 1
                else:
                    kept_rows.append(row)
            self.db.write_rows(table_name, kept_rows)
            output_stream.write(f"( {deleted_count} rows)\n")
        else:
            deleted_count = len(all_rows)
            self.db.write_rows(table_name, [])
            output_stream.write(f"( {deleted_count} rows)\n")

    def do_update(self, tokens: list[str], output_stream):
        if len(tokens) < 4 or tokens[2].lower() != "set":
            sys.stderr.write("Improper syntax.\n")
            return

        table_name = tokens[1].lower()
        if self.db.view_exists(table_name):
            sys.stderr.write("Can not update a view.\n")
            return
        if not self.db.table_exists(table_name):
            sys.stderr.write(f"{tokens[1]} does not exist.\n")
            return

        schema = self.db.get_table_schema(table_name)
        col_map = {name.lower(): i for i, (name, _) in enumerate(schema)}

        where_idx = -1
        for i in range(3, len(tokens)):
            if tokens[i].lower() == "where":
                where_idx = i
                break

        set_tokens = tokens[3:where_idx] if where_idx != -1 else tokens[3:]
        where_tokens = tokens[where_idx + 1 :] if where_idx != -1 else []

        assignments = []
        i = 0
        while i < len(set_tokens):
            cname = set_tokens[i].lower()
            if cname not in col_map:
                sys.stderr.write(f"Bad field name ({cname})\n")
                return
            if i + 1 >= len(set_tokens) or set_tokens[i + 1] != "=":
                sys.stderr.write("Improper syntax.\n")
                return
            val = set_tokens[i + 2]
            assignments.append((col_map[cname], strip_quotes(val)))
            i += 3

        all_rows = self.db.read_rows(table_name)
        updated_count = 0
        new_rows = []

        for row in all_rows:
            if not where_tokens or self.eval_where_condition(where_tokens, schema, row, table_name):
                row_copy = list(row)
                for col_i, new_val in assignments:
                    row_copy[col_i] = new_val
                new_rows.append(row_copy)
                updated_count += 1
            else:
                new_rows.append(row)

        self.db.write_rows(table_name, new_rows)
        output_stream.write(f"( {updated_count} rows)\n")

    def do_help(self, tokens: list[str], output_stream):
        if len(tokens) < 2:
            sys.stderr.write("Ambiguous syntax, try: HELP ALL or HELP TABLES or HELP VIEWS or HELP COMMANDS\n")
            return

        arg = tokens[1].lower()
        if arg == "all":
            names = sorted(list(set(self.get_all_tables() + self.get_all_views())))
            for n in names:
                output_stream.write(f"{n}\n")
        elif arg == "tables":
            views = set(self.get_all_views())
            tables = sorted([t for t in self.get_all_tables() if t not in views])
            for t in tables:
                output_stream.write(f"{t}\n")
        elif arg == "views":
            for v in sorted(self.get_all_views()):
                output_stream.write(f"{v}\n")
        elif arg == "commands":
            output_stream.write("/p is print\n/g is go(execute)\n/q is quit\n/e is edit\n/i is include\n/w is write\n/r is reset(clear)\n/s is shell\n/y is python shell\n/p/g print and go\nThe number sign(#) may be used at the start of a line for comments.\n")
        elif arg in ("create", "delete", "drop", "edit", "help", "insert", "print", "select", "update", "where", "syntax"):
            self.print_syntax_help(arg, output_stream)
        else:
            table_name = arg
            if self.db.table_exists(table_name) or self.db.view_exists(table_name):
                output_stream.write(f"\n <{table_name}>\n")
                schema = self.get_effective_schema(table_name)
                for cname, cwidth in schema:
                    output_stream.write(f"{cname}\t{cwidth}\n")

                view_def = self.db.get_view_def(table_name)
                if view_def:
                    t1, f1, t2, f2, _ = view_def
                    output_stream.write(f"\n View:\t{t1}.{f1} = {t2}.{f2}\n")

                rows = self.get_effective_rows(table_name)
                output_stream.write(f"\n Rows:\t{len(rows)}\n")
            else:
                sys.stderr.write(f"{table_name} does not exist.\n")

    def print_syntax_help(self, topic: str, output_stream):
        syntax_msgs = {
            "create": "CREATE TABLE table_name (\n\tcolumn_name column_width \n\t{, ...} \n) \nor\nCREATE VIEW view_name (\n\ttable_or_view1.column1 = table_or_view2.column2\n)\n",
            "delete": "DELETE \nFROM table_name\t\n{ WHERE where_clause }\n",
            "drop": "DROP TABLE table_name\nor\nDROP VIEW view_name\n",
            "edit": "EDIT table_name\nis a non-standard method of changing a table's field names or display widths.\n",
            "help": "HELP ALL\nor\nHELP TABLES \nor\nHELP VIEWS\nor\nHELP COMMANDS\nor\nHELP [CREATE | DELETE | DROP | INSERT | SELECT | UPDATE | WHERE | PRINT | EDIT]\nor\nHELP table_name\nCommands must appear in lower case.\n",
            "insert": "INSERT INTO table_name \n\t{ ( column_name, ... ) }\nVALUES ( expression, ...)\nor\t\t\nINSERT INTO table_name \n\t{ ( column_name, ... ) }\nsubselect\n",
            "print": "PRINT table_name \nis a non-standard synonym for SELECT * FROM table_name.\n",
            "select": "SELECT { DISTINCT } \n\t[ column_name {,...} | * ]\nFROM [ table_name | view_name ]\n{ WHERE where_clause }\n{ ORDER BY column_name { NUM } { ASC | DESC } {, ... }\n{ UNION select statement }\n'NUM' is a non-standard method for sorting numeric fields.\n",
            "update": "UPDATE table_name\nSET column_name = expression {, ... }\n{ WHERE where_clause }\n",
            "where": "WHERE [ column_name | value ] [ =, !=, >, <, >=, <=, and, or, not, in ]\n      [ column_name | value | subselect ]\nParentheses may be used to group expressions.  \n",
        }
        if topic in syntax_msgs:
            output_stream.write(syntax_msgs[topic])

    def do_print(self, tokens: list[str], output_stream):
        if len(tokens) < 2:
            sys.stderr.write("Improper syntax (print)\n")
            return
        table_name = tokens[1]
        select_tokens = ["select", "*", "from", table_name]
        res = self.do_select(select_tokens)
        if res is not None:
            output_stream.write(res)

    def get_all_tables(self) -> list[str]:
        res = []
        for f in os.listdir(self.db.db_dir):
            if f.endswith(".meta"):
                res.append(f[:-5])
        return res

    def get_all_views(self) -> list[str]:
        res = []
        for f in os.listdir(self.db.db_dir):
            if f.endswith(".view"):
                res.append(f[:-5])
        return res

    def get_effective_schema(self, table_or_view: str) -> list[tuple[str, int]]:
        table_or_view = table_or_view.lower()
        if self.db.view_exists(table_or_view):
            view_def = self.db.get_view_def(table_or_view)
            if view_def:
                t1, f1, t2, f2, _ = view_def
                s1 = self.get_effective_schema(t1)
                s2 = self.get_effective_schema(t2)
                return s1 + s2
        return self.db.get_table_schema(table_or_view)

    def get_effective_rows(self, table_or_view: str) -> list[list[str]]:
        table_or_view = table_or_view.lower()
        if self.db.view_exists(table_or_view):
            view_def = self.db.get_view_def(table_or_view)
            if view_def:
                t1, f1, t2, f2, _ = view_def
                r1 = self.get_effective_rows(t1)
                r2 = self.get_effective_rows(t2)
                s1 = self.get_effective_schema(t1)
                s2 = self.get_effective_schema(t2)

                idx1 = next((i for i, (n, _) in enumerate(s1) if n.lower() == f1.lower()), -1)
                idx2 = next((i for i, (n, _) in enumerate(s2) if n.lower() == f2.lower()), -1)

                joined = []
                for row1 in r1:
                    val1 = row1[idx1] if idx1 < len(row1) else ""
                    for row2 in r2:
                        val2 = row2[idx2] if idx2 < len(row2) else ""
                        if compare_vals(val1, "==", val2):
                            joined.append(row1 + row2)
                return joined
        return self.db.read_rows(table_or_view)

    # -------------------------------------------------------------
    # SELECT & WHERE IMPLEMENTATION
    # -------------------------------------------------------------

    def exec_select_query(self, tokens: list[str]) -> tuple[list[tuple[str, int]], list[list[str]], bool] | None:
        """Executes SELECT query and returns (headers, rows, is_agg)."""
        if not tokens or tokens[0].lower() != "select":
            sys.stderr.write("Syntax error.\n")
            return None

        from_idx = -1
        for i, tok in enumerate(tokens):
            if tok.lower() == "from":
                from_idx = i
                break
        if from_idx == -1:
            sys.stderr.write("Syntax error.\n")
            return None

        where_idx = -1
        order_idx = -1
        for i in range(from_idx + 1, len(tokens)):
            tok_l = tokens[i].lower()
            if tok_l == "where" and where_idx == -1:
                where_idx = i
            elif tok_l == "order" and order_idx == -1 and i + 1 < len(tokens) and tokens[i + 1].lower() == "by":
                order_idx = i

        tables_end = len(tokens)
        if where_idx != -1:
            tables_end = min(tables_end, where_idx)
        if order_idx != -1:
            tables_end = min(tables_end, order_idx)

        table_names = [t.lower() for t in tokens[from_idx + 1 : tables_end]]
        if not table_names:
            sys.stderr.write("Syntax error.\n")
            return None

        for tname in table_names:
            if not self.db.table_exists(tname) and not self.db.view_exists(tname):
                sys.stderr.write(f"{tname} does not exist.\n")
                return None

        where_tokens = []
        if where_idx != -1:
            where_end = order_idx if order_idx != -1 else len(tokens)
            where_tokens = tokens[where_idx + 1 : where_end]

        combined_schema, combined_rows = self.combine_tables(table_names, where_tokens)
        if combined_schema is None:
            return None

        filtered_rows = []
        for r in combined_rows:
            if not where_tokens or self.eval_where_condition(where_tokens, combined_schema, r, table_names[0]):
                filtered_rows.append(r)

        proj_tokens = tokens[1:from_idx]
        is_distinct = False
        if proj_tokens and proj_tokens[0].lower() == "distinct":
            is_distinct = True
            proj_tokens = proj_tokens[1:]

        agg_specs = []
        p_i = 0
        while p_i < len(proj_tokens):
            tok = proj_tokens[p_i].lower()
            if tok in ("sum", "avg", "count", "min", "max"):
                if p_i + 3 < len(proj_tokens) and proj_tokens[p_i + 1] == "(" and proj_tokens[p_i + 3] == ")":
                    col = proj_tokens[p_i + 2].lower()
                    agg_specs.append((tok, col))
                    p_i += 4
                    continue
            m_agg = re.match(r"^(sum|avg|count|min|max)\s*\(\s*([^\s\)]+)\s*\)$", tok, re.IGNORECASE)
            if m_agg:
                agg_specs.append((m_agg.group(1).lower(), m_agg.group(2).lower()))
                p_i += 1
                continue
            p_i += 1

        if agg_specs:
            agg_headers = []
            agg_vals = []
            for func, col_name in agg_specs:
                idx = self.find_col_idx(combined_schema, col_name)
                if idx == -1:
                    sys.stderr.write(f"Bad field name ({col_name})\n")
                    return None
                col_w = max(combined_schema[idx][1], 10)
                agg_headers.append((col_name, col_w))

                raw_vals = [r[idx] for r in filtered_rows if idx < len(r)]
                num_vals = []
                for v in raw_vals:
                    try:
                        num_vals.append(float(v))
                    except ValueError:
                        pass

                if func == "count":
                    val_str = str(len(raw_vals))
                elif func == "sum":
                    val_str = str(int(sum(num_vals))) if all(v.is_integer() for v in num_vals) else str(sum(num_vals))
                elif func == "avg":
                    avg_v = sum(num_vals) / len(num_vals) if num_vals else 0.0
                    val_str = f"{avg_v:.1f}" if avg_v % 1 != 0 else str(int(avg_v))
                elif func == "min":
                    val_str = str(min(raw_vals)) if raw_vals else ""
                elif func == "max":
                    val_str = str(max(raw_vals)) if raw_vals else ""
                else:
                    val_str = ""
                agg_vals.append(val_str)

            return agg_headers, [agg_vals], True

        selected_indices = []
        out_headers = []

        if len(proj_tokens) == 1 and proj_tokens[0] == "*":
            selected_indices = list(range(len(combined_schema)))
            out_headers = combined_schema
        else:
            for tok in proj_tokens:
                idx = self.find_col_idx(combined_schema, tok)
                if idx == -1:
                    sys.stderr.write(f"Bad field name ({tok})\n")
                    return None
                selected_indices.append(idx)
                out_headers.append(combined_schema[idx])

        if order_idx != -1:
            order_tokens = tokens[order_idx + 2 :]
            i = 0
            sort_keys = []
            while i < len(order_tokens):
                cname = order_tokens[i].lower()
                c_idx = self.find_col_idx(combined_schema, cname)
                if c_idx == -1:
                    sys.stderr.write(f"Bad field name ({cname})\n")
                    return None
                i += 1
                is_num = False
                is_desc = False
                while i < len(order_tokens) and order_tokens[i].lower() in ("num", "asc", "desc"):
                    if order_tokens[i].lower() == "num":
                        is_num = True
                    elif order_tokens[i].lower() == "desc":
                        is_desc = True
                    i += 1
                sort_keys.append((c_idx, is_num, is_desc))

            def row_sort_key(r):
                key = []
                for idx, is_num, is_desc in sort_keys:
                    val = r[idx] if idx < len(r) else ""
                    if is_num:
                        try:
                            v = float(val)
                        except ValueError:
                            v = 0.0
                    else:
                        v = val
                    key.append(v)
                return tuple(key)

            reverse = sort_keys[0][2] if sort_keys else False
            filtered_rows.sort(key=row_sort_key, reverse=reverse)

        proj_rows = []
        for r in filtered_rows:
            proj_rows.append([r[i] if i < len(r) else "" for i in selected_indices])

        if is_distinct:
            seen = set()
            distinct_rows = []
            for r in proj_rows:
                t_r = tuple(r)
                if t_r not in seen:
                    seen.add(t_r)
                    distinct_rows.append(r)
            proj_rows = distinct_rows

        return out_headers, proj_rows, False

    def do_select(self, tokens: list[str]) -> str | None:
        union_indices = [i for i, tok in enumerate(tokens) if tok.lower() == "union"]
        if union_indices:
            part1 = tokens[: union_indices[0]]
            part2 = tokens[union_indices[0] + 1 :]
            out1 = self.do_select(part1)
            out2 = self.do_select(part2)
            res = ""
            if out1:
                res += out1
            if out2:
                res += out2
            return res

        res_tuple = self.exec_select_query(tokens)
        if res_tuple is None:
            return None
        headers, rows, is_agg = res_tuple
        return format_table_output(headers, rows, quiet=self.quiet, is_agg=is_agg)

    def eval_select_rows(self, tokens: list[str]) -> list[list[str]] | None:
        """Returns rows from subselect query for subselect/insert expressions."""
        res_tuple = self.exec_select_query(tokens)
        if res_tuple is None:
            return []
        _, rows, _ = res_tuple
        return rows

    def find_col_idx(self, schema: list[tuple[str, int]], col_name: str) -> int:
        col_name = col_name.lower()
        if "." in col_name:
            tbl, col = col_name.split(".", 1)
            for idx, (n, _) in enumerate(schema):
                if n.lower() == col or n.lower() == col_name:
                    return idx
        for idx, (n, _) in enumerate(schema):
            if n.lower() == col_name:
                return idx
        return -1

    def combine_tables(self, table_names: list[str], where_tokens: list[str]) -> tuple[list[tuple[str, int]] | None, list[list[str]]]:
        current_schema = self.get_effective_schema(table_names[0])
        current_rows = self.get_effective_rows(table_names[0])

        for tname in table_names[1:]:
            next_schema = self.get_effective_schema(tname)
            next_rows = self.get_effective_rows(tname)

            join_cond = self.find_join_condition(where_tokens, current_schema, next_schema, tname)
            if join_cond:
                idx1, idx2 = join_cond
                new_rows = []
                for r1 in current_rows:
                    v1 = r1[idx1] if idx1 < len(r1) else ""
                    for r2 in next_rows:
                        v2 = r2[idx2] if idx2 < len(r2) else ""
                        if compare_vals(v1, "==", v2):
                            new_rows.append(r1 + r2)
                current_rows = new_rows
            else:
                new_rows = []
                for r1 in current_rows:
                    for r2 in next_rows:
                        new_rows.append(r1 + r2)
                current_rows = new_rows

            current_schema = current_schema + next_schema

        return current_schema, current_rows

    def find_join_condition(self, where_tokens: list[str], s1: list[tuple[str, int]], s2: list[tuple[str, int]], t2_name: str) -> tuple[int, int] | None:
        for i in range(len(where_tokens) - 2):
            if where_tokens[i + 1] == "=":
                left = where_tokens[i]
                right = where_tokens[i + 2]
                idx1_l = self.find_col_idx(s1, left)
                idx2_r = self.find_col_idx(s2, right)
                if idx1_l != -1 and idx2_r != -1:
                    return (idx1_l, idx2_r)
                idx2_l = self.find_col_idx(s2, left)
                idx1_r = self.find_col_idx(s1, right)
                if idx2_l != -1 and idx1_r != -1:
                    return (idx1_r, idx2_l)
        return None

    def eval_where_condition(self, tokens: list[str], schema: list[tuple[str, int]], row: list[str], table_name: str) -> bool:
        if not tokens:
            return True

        sel_idx = -1
        for i, tok in enumerate(tokens):
            if tok.lower() == "select":
                sel_idx = i
                break

        if sel_idx != -1:
            lhs_tokens = tokens[:sel_idx]
            select_tokens = tokens[sel_idx:]

            sub_rows = self.eval_select_rows(select_tokens)
            sub_vals = [r[0] for r in sub_rows if r] if sub_rows else []

            if "in" in [t.lower() for t in lhs_tokens]:
                in_pos = next(idx for idx, t in enumerate(lhs_tokens) if t.lower() == "in")
                is_not = in_pos - 1 >= 0 and lhs_tokens[in_pos - 1].lower() == "not"
                if is_not:
                    col_name = lhs_tokens[in_pos - 2] if in_pos - 2 >= 0 else ""
                else:
                    col_name = lhs_tokens[in_pos - 1] if in_pos - 1 >= 0 else ""

                c_idx = self.find_col_idx(schema, col_name)
                val = row[c_idx] if c_idx != -1 and c_idx < len(row) else ""
                if is_not:
                    return not any(compare_vals(val, "==", sv) for sv in sub_vals)
                else:
                    return any(compare_vals(val, "==", sv) for sv in sub_vals)

            elif "=" in lhs_tokens:
                eq_pos = next(idx for idx, t in enumerate(lhs_tokens) if t == "=")
                col_name = lhs_tokens[eq_pos - 1]
                c_idx = self.find_col_idx(schema, col_name)
                val = row[c_idx] if c_idx != -1 and c_idx < len(row) else ""
                target_val = sub_vals[0] if sub_vals else ""
                return compare_vals(val, "==", target_val)

        i = 0
        n = len(tokens)
        expr = ""
        while i < n:
            tok = tokens[i]
            tok_l = tok.lower()

            if tok_l in ("and", "or", "not", "(", ")"):
                expr += f" {tok_l} "
                i += 1
            elif i + 1 < n and tokens[i + 1] in ("=", "!=", ">", "<", ">=", "<="):
                lhs_tok = tokens[i]
                op = tokens[i + 1]
                rhs_tok = tokens[i + 2]

                c_idx_l = self.find_col_idx(schema, lhs_tok)
                lhs_v = row[c_idx_l] if c_idx_l != -1 and c_idx_l < len(row) else strip_quotes(lhs_tok)

                c_idx_r = self.find_col_idx(schema, rhs_tok)
                rhs_v = row[c_idx_r] if c_idx_r != -1 and c_idx_r < len(row) else strip_quotes(rhs_tok)

                cond_res = compare_vals(lhs_v, op, rhs_v)
                expr += f" {cond_res} "
                i += 3
            else:
                i += 1

        try:
            return bool(eval(expr))
        except Exception:
            return True


class REPL:
    """Interactive SQL REPL with slash command processing."""

    def __init__(self, db: Database, quiet: bool = False):
        self.db = db
        self.quiet = quiet
        self.engine = QueryEngine(db, quiet=quiet)
        self.cmd_buffer = ""
        self.is_new = False

    def run(self, input_stream=sys.stdin, output_stream=sys.stdout):
        while True:
            while True:
                output_stream.write("* ")
                output_stream.flush()
                try:
                    line = input_stream.readline()
                except (IOError, EOFError):
                    line = ""
                if not line:
                    output_stream.write("Goodbye\n")
                    return

                line = line.rstrip("\r\n")

                m_pg = re.match(r"^(.*?)(/p/g|/pg)\s*$", line, re.IGNORECASE)
                m_slash = re.match(r"^(.*?)(/[a-z])\s*$", line, re.IGNORECASE) if not m_pg else None

                if m_pg:
                    sql_part = m_pg.group(1)
                    if self.is_new:
                        self.cmd_buffer = ""
                    if sql_part:
                        self.cmd_buffer += sql_part + "\n"
                    line = "/p/g"
                    self.is_new = False
                elif m_slash:
                    sql_part = m_slash.group(1)
                    if self.is_new:
                        self.cmd_buffer = ""
                    if sql_part:
                        self.cmd_buffer += sql_part + "\n"
                    line = m_slash.group(2)
                    self.is_new = False

                cmd_lower = line.strip().lower()

                if cmd_lower in ("/p", "p"):
                    output_stream.write(self.cmd_buffer)
                    output_stream.flush()
                elif cmd_lower in ("/g", "g"):
                    break
                elif cmd_lower in ("/p/g", "pg"):
                    output_stream.write(self.cmd_buffer)
                    output_stream.flush()
                    break
                elif cmd_lower in ("/r", "r"):
                    output_stream.write("reset\n")
                    output_stream.flush()
                    self.cmd_buffer = ""
                elif cmd_lower in ("/s", "s"):
                    shell = os.environ.get("SHELL", "/bin/sh")
                    subprocess.run(shell, shell=True)
                elif cmd_lower in ("/y", "y", "/py", "py"):
                    output_stream.write("Entering Python Shell (type exit() to return):\n")
                    output_stream.flush()
                    code.interact(banner="", local={"db": self.db, "engine": self.engine})
                elif cmd_lower in ("/e", "e"):
                    editor = os.environ.get("EDITOR")
                    if editor:
                        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
                            tf.write(self.cmd_buffer)
                            tf_path = tf.name
                        try:
                            subprocess.run(f"{editor} {tf_path}", shell=True)
                            with open(tf_path, "r") as tf:
                                self.cmd_buffer = tf.read()
                        finally:
                            if os.path.exists(tf_path):
                                os.remove(tf_path)
                    else:
                        output_stream.write("Enter new buffer text (end with EOF / Ctrl+D):\n")
                        output_stream.flush()
                        new_buf = input_stream.read()
                        if new_buf:
                            self.cmd_buffer = new_buf
                elif cmd_lower in ("/i", "i"):
                    output_stream.write("Enter include file: ")
                    output_stream.flush()
                    inc_file = input_stream.readline().rstrip("\r\n")
                    if os.path.exists(inc_file):
                        with open(inc_file, "r") as f:
                            self.cmd_buffer += f.read() + "\n"
                        output_stream.write(f"{inc_file} included\n")
                        output_stream.flush()
                elif cmd_lower in ("/w", "w"):
                    output_stream.write("Enter output file: ")
                    output_stream.flush()
                    out_file = input_stream.readline().rstrip("\r\n")
                    if out_file:
                        with open(out_file, "w") as f:
                            f.write(self.cmd_buffer)
                        output_stream.write(f"{out_file} written\n")
                        output_stream.flush()
                elif cmd_lower in ("/q", "q"):
                    output_stream.write("Goodbye\n")
                    output_stream.flush()
                    return
                elif line.strip().startswith("#"):
                    if self.is_new:
                        self.cmd_buffer = ""
                else:
                    if self.is_new:
                        self.cmd_buffer = ""
                    self.cmd_buffer += line + "\n"

                self.is_new = False

            sql_to_eval = eval_backticks(self.cmd_buffer.strip())
            if sql_to_eval:
                self.execute_sql(sql_to_eval, output_stream)
            self.is_new = True

    def execute_sql(self, sql: str, output_stream):
        tokens = tokenize_sql(sql)
        if not tokens:
            return
        self.engine.execute(tokens, output_stream)


class TestShql(unittest.TestCase):
    """Built-in test suite for shql.py."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_csv_table_crud(self):
        self.db.create_table("users", [("id", 5), ("name", 20)])
        self.assertTrue(self.db.table_exists("users"))
        self.assertEqual(self.db.get_table_schema("users"), [("id", 5), ("name", 20)])

        self.db.append_rows("users", [["1", "Alice"], ["2", "Bob"]])
        rows = self.db.read_rows("users")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ["1", "Alice"])

        self.db.drop_table("users")
        self.assertFalse(self.db.table_exists("users"))

    def test_sql_engine_queries(self):
        engine = QueryEngine(self.db)
        out = io.StringIO()

        engine.execute(tokenize_sql("create table items (id 5, price 10)"), out)
        engine.execute(tokenize_sql("insert into items values ('1', '10.5', '2', '20.0')"), out)

        res = engine.do_select(tokenize_sql("select * from items where price > 15"))
        self.assertIn("20.0", res)
        self.assertNotIn("10.5", res)

        res_agg = engine.do_select(tokenize_sql("select sum(price), avg(price) from items"))
        self.assertIn("30.5", res_agg)
        self.assertIn("15.2", res_agg)

        res_sub = engine.do_select(tokenize_sql("select id from items where price = select min(price) from items"))
        self.assertIn("1", res_sub)


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(description="shql flat-file SQL interpreter")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet mode (no headings)")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("database", nargs="?", help="Database directory name")
    return parser.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])
    if args.test:
        suite = unittest.TestLoader().loadTestsFromTestCase(TestShql)
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)

    if not args.database:
        sys.stderr.write("Missing database name.\n")
        sys.stderr.write("The database name must be a directory under $HOME/shql\n")
        sys.stderr.write(f"  or a directory under {SHQL_ROOT}\n")
        sys.exit(1)

    try:
        db = Database(args.database)
    except FileNotFoundError as e:
        sys.stderr.write(f"Unknown database ({args.database})\n")
        sys.stderr.write("The database name must be a directory under $HOME/shql\n")
        sys.stderr.write(f"  or a directory under {SHQL_ROOT}\n")
        sys.exit(1)

    print(f"Database: {args.database}")
    repl = REPL(db, quiet=args.quiet)
    repl.run()


if __name__ == "__main__":
    main()
