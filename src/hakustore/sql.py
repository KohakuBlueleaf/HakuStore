import sqlite3
import threading  # For thread-local connections if needed, or manage externally
from abc import ABC, abstractmethod
from enum import Enum
from hakustore.logger import logger


DOUBLE_QUOTES = '""'  # For quoting identifiers in SQL queries
SINGLE_QUOTES = '"'  # For string literals in SQL queries


class DBType(Enum):
    SQLITE = "sqlite"


class BaseSQLHandler(ABC):
    def __init__(self, db_path: str, table_name: str, read_only: bool = False):
        self.db_path = db_path
        self.table_name = (
            table_name  # Keep original for reference, use quoted for queries
        )
        self.read_only = read_only
        self.conn = None
        self._lock = (
            threading.RLock()
        )  # Basic re-entrant lock for connection operations

    @abstractmethod
    def connect(self):
        pass

    def close(self):
        with self._lock:
            if self.conn:
                try:
                    self.conn.close()
                    logger.debug(f"Closed connection to {self.db_path}")
                except Exception as e:
                    logger.error(f"Error closing connection to {self.db_path}: {e}")
                finally:
                    self.conn = None
            else:
                logger.debug(f"No active connection to close for {self.db_path}")

    def execute_query(
        self,
        query: str,
        params: tuple = None,
        fetch_one: bool = False,
        fetch_all: bool = False,
        commit: bool = False,
        get_cursor_description: bool = False,
    ):
        with self._lock:
            if not self.conn:
                self.connect()
            if not self.conn:  # Still no connection after trying
                raise ConnectionError(
                    f"Failed to establish database connection to {self.db_path}"
                )

            cursor = None
            try:
                cursor = self.conn.cursor()
                logger.debug(f"Executing query: {query} with params: {params}")
                cursor.execute(query, params or ())

                result = None
                description = None

                if get_cursor_description:
                    description = cursor.description

                if fetch_one:
                    result = cursor.fetchone()
                elif fetch_all:
                    result = cursor.fetchall()

                if commit:
                    self.conn.commit()

                if get_cursor_description:
                    return result, description
                return result
            except Exception as e:
                logger.error(
                    f"Error executing query '{query}' with params {params}: {e}"
                )
                # Optionally rollback on error, though for simple ops, commit is explicit
                # if self.conn and not self.read_only:
                #     self.conn.rollback()
                raise
            finally:
                if cursor:
                    cursor.close()

    @abstractmethod
    def table_exists(self, table_name: str) -> bool:
        pass

    @abstractmethod
    def get_table_columns(self, table_name: str) -> dict:  # {name: type_str}
        pass

    @abstractmethod
    def add_table_column(self, table_name: str, column_name: str, column_type_str: str):
        pass

    @abstractmethod
    def create_table(
        self,
        table_name: str,
        columns_with_types: dict,
        primary_key_column: str = None,
        unique_constraints: list = None,
    ):
        pass

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        pass

    @abstractmethod
    def map_generic_type_to_specific(self, generic_type: str) -> str:
        """Maps 'TEXT', 'INTEGER', 'REAL', 'BLOB' to DB specific types."""
        pass

    @abstractmethod
    def get_insert_or_replace_sql(
        self, table_name: str, data_dict: dict, id_column_name: str
    ) -> tuple[str, tuple]:
        """
        Generates SQL for INSERT OR REPLACE (SQLite) or INSERT ON CONFLICT DO UPDATE (DuckDB).
        Returns (sql_query_string, params_tuple).
        `data_dict` keys are column names (already Python strings), values are Python values to be stored.
        `id_column_name` is the name of the primary key column.
        """
        pass


class SQLiteHandler(BaseSQLHandler):
    def connect(self):
        with self._lock:
            if self.conn:
                return
            try:
                # isolation_level=None for autocommit can be simpler for some use cases,
                # but explicit commit is generally safer.
                # timeout for busy db
                self.conn = sqlite3.connect(self.db_path, timeout=10.0)
                self.conn.row_factory = sqlite3.Row  # Access columns by name
                logger.debug(f"Connected to SQLite DB: {self.db_path}")
            except sqlite3.Error as e:
                logger.error(f"Error connecting to SQLite DB {self.db_path}: {e}")
                self.conn = None  # Ensure conn is None if connection failed
                raise

    def table_exists(self, table_name: str) -> bool:
        q_table_name = self.quote_identifier(
            table_name
        )  # Table name isn't parameterized
        query = f"SELECT name FROM sqlite_master WHERE type='table' AND name=?;"
        # sqlite_master name column is case-sensitive as stored.
        result = self.execute_query(query, (table_name,), fetch_one=True)
        return result is not None

    def get_table_columns(self, table_name: str) -> dict:
        # PRAGMA table_info is not parameterized for table_name directly.
        # Ensure table_name is safe if it comes from user input, though here it's from constructor.
        if not table_name.isalnum() and "_" not in table_name:  # Basic sanity check
            raise ValueError(f"Invalid table name for PRAGMA: {table_name}")
        query = f"PRAGMA table_info({self.quote_identifier(table_name)})"  # quote_identifier might not be needed for PRAGMA argument
        try:
            # PRAGMA statements cannot be parameterized in the same way as DML/SELECT
            # This is a known limitation/characteristic of SQLite.
            # The table_name is initialized with the class and should be controlled.
            # Using execute_query for connection management and cursor handling.
            # This specific call bypasses param substitution for the PRAGMA.
            with self._lock:
                if not self.conn:
                    self.connect()
                cursor = self.conn.cursor()
                cursor.execute(f"PRAGMA table_info({table_name})")  # Raw table name
                columns_info = cursor.fetchall()
                cursor.close()
        except sqlite3.Error as e:
            logger.error(f"Failed to get table_info for {table_name}: {e}")
            return {}

        # col_info: (cid, name, type, notnull, dflt_value, pk)
        return {info[1]: info[2].upper() for info in columns_info}

    def add_table_column(
        self, table_name: str, column_name: str, generic_column_type: str
    ):
        q_table = self.quote_identifier(table_name)
        q_col = self.quote_identifier(column_name)
        db_col_type = self.map_generic_type_to_specific(generic_column_type)
        query = f"ALTER TABLE {q_table} ADD COLUMN {q_col} {db_col_type}"
        self.execute_query(query, commit=True)
        logger.debug(
            f"Added column {column_name} ({db_col_type}) to table {table_name}"
        )

    def create_table(
        self,
        table_name: str,
        columns_with_types: dict,
        primary_key_column: str = None,
        unique_constraints: list = None,
    ):
        q_table = self.quote_identifier(table_name)
        defs = []
        for name, generic_type in columns_with_types.items():
            q_name = self.quote_identifier(name)
            db_type = self.map_generic_type_to_specific(generic_type)
            col_def = f"{q_name} {db_type}"
            if name == primary_key_column:
                col_def += " PRIMARY KEY"
            defs.append(col_def)

        if unique_constraints:
            for constraint_cols in unique_constraints:
                q_constraint_cols = ", ".join(
                    [self.quote_identifier(c) for c in constraint_cols]
                )
                defs.append(f"UNIQUE ({q_constraint_cols})")

        query = f"CREATE TABLE IF NOT EXISTS {q_table} ({', '.join(defs)})"
        self.execute_query(query, commit=True)
        logger.debug(
            f"Ensured table {table_name} exists with columns: {columns_with_types}"
        )

    def quote_identifier(self, name: str) -> str:
        return f'"{name.replace(SINGLE_QUOTES, DOUBLE_QUOTES)}"'  # Standard SQL quoting for SQLite

    def map_generic_type_to_specific(self, generic_type: str) -> str:
        # SQLite types are flexible, these are common affinities
        return generic_type.upper()

    def get_insert_or_replace_sql(
        self, table_name: str, data_dict: dict, id_column_name: str
    ) -> tuple[str, tuple]:
        q_table = self.quote_identifier(table_name)

        # Ensure id_column_name is in data_dict if it's part of the values
        # This method assumes data_dict already contains the value for id_column_name

        columns = [self.quote_identifier(k) for k in data_dict.keys()]
        placeholders = ["?" for _ in data_dict]
        values = tuple(data_dict.values())  # Order is preserved from Python 3.7+ dicts

        sql = f"INSERT OR REPLACE INTO {q_table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        return sql, values


def get_sql_handler(
    db_type_str: str, db_path: str, table_name: str, **kwargs
) -> BaseSQLHandler:
    db_type = DBType(db_type_str.lower())
    if db_type == DBType.SQLITE:
        return SQLiteHandler(db_path, table_name, **kwargs)
    else:
        raise ValueError(f"Unsupported DB type: {db_type_str}")
