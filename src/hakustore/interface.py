import collections.abc
from abc import abstractmethod  # For PersistentBase

from hakustore.logger import logger
from hakustore.sql import get_sql_handler, BaseSQLHandler
from hakustore.utils import (
    get_db_type_and_serialized_value,
    deserialize_db_value,
    SQL_NULL_SENTINEL,
)


class PersistentBase:
    _INTERNAL_ORDER_COLUMN_LIST = "__hakustore_internal_order__"

    def __init__(
        self,
        db_path: str = ":memory:",
        table_name: str = "hakustore_dict",
        db_type: str = "sqlite",
        read_only: bool = False,
        **kwargs,
    ):
        self.db_path = db_path
        self.table_name = (
            table_name  # User-provided, might need sanitization if used in PRAGMAs raw
        )
        self.db_type = db_type
        self.read_only = read_only

        # Table name for SQL queries should be quoted by the handler
        self.sql_handler: BaseSQLHandler = get_sql_handler(
            db_type, db_path, table_name, read_only=read_only, **kwargs
        )

        self._columns_cache: dict | None = None  # Cache of {col_name: col_type_str}
        self._init_db_structure()

    @abstractmethod
    def _init_db_structure(self):
        """Ensure table and essential columns exist. Implemented by subclasses."""
        pass

    def _get_current_columns(self, use_cache=True) -> dict:
        if use_cache and self._columns_cache is not None:
            return self._columns_cache

        if not self.sql_handler.table_exists(self.table_name):
            # Table might not exist yet if _init_db_structure hasn't fully run or failed
            # Or if this is called before table creation (e.g. during it)
            return {}

        self._columns_cache = self.sql_handler.get_table_columns(self.table_name)
        return self._columns_cache

    def _ensure_columns(self, data_dict: dict):
        """Ensures all keys in data_dict exist as columns in the table."""
        if self.read_only:
            # Sanity check, though writes should be blocked earlier
            # Check if all keys in data_dict are already columns
            current_cols = self._get_current_columns()
            for key in data_dict.keys():
                if (
                    key not in current_cols and key != self._INTERNAL_ORDER_COLUMN_LIST
                ):  # Order col is special
                    logger.warning(
                        f"Read-only mode: Column '{key}' for input data does not exist and cannot be added."
                    )
            return

        current_cols = self._get_current_columns(
            use_cache=False
        )  # Force refresh before potential adds
        new_columns_added = False
        for key, value in data_dict.items():
            if (
                key not in current_cols and key != self._INTERNAL_ORDER_COLUMN_LIST
            ):  # Internal order col managed separately
                if not isinstance(key, str):
                    raise TypeError(
                        f"Dictionary keys to be stored as columns must be strings. Got: {key} ({type(key)})"
                    )

                _serialized_val, generic_type = get_db_type_and_serialized_value(value)
                logger.debug(
                    f"Column '{key}' not found. Adding with generic type '{generic_type}'."
                )
                self.sql_handler.add_table_column(self.table_name, key, generic_type)
                new_columns_added = True

        if new_columns_added:
            self._columns_cache = self.sql_handler.get_table_columns(
                self.table_name
            )  # Update cache

    def _prepare_data_for_db(self, data_dict: dict) -> dict:
        """Serializes values in data_dict for DB insertion."""
        db_ready_dict = {}
        for key, value in data_dict.items():
            serialized_value, _ = get_db_type_and_serialized_value(value)
            db_ready_dict[key] = serialized_value
        return db_ready_dict

    def _process_row_to_dict(
        self, row_data, column_names_from_cursor: list, column_types_from_schema: dict
    ) -> dict:
        """
        Converts a DB row (tuple or sqlite3.Row) to a Python dict, deserializing values.
        `row_data`: The raw row from the cursor.
        `column_names_from_cursor`: List of column names from cursor.description.
        `column_types_from_schema`: Dict of {col_name: col_db_type_str} from get_table_columns.
        """
        item_dict = {}
        for i, col_name in enumerate(column_names_from_cursor):
            # Skip internal columns unless specifically handled by a subclass (like PersistentDict's ID)
            if col_name == self._INTERNAL_ORDER_COLUMN_LIST:
                continue

            raw_db_value = (
                row_data[i] if isinstance(row_data, tuple) else row_data[col_name]
            )

            # Get the declared type of the column from schema for robust deserialization
            declared_col_type = column_types_from_schema.get(
                col_name, "TEXT"
            )  # Default to TEXT if somehow not in schema cache

            deserialized_value = deserialize_db_value(raw_db_value, declared_col_type)

            if deserialized_value is not SQL_NULL_SENTINEL:
                item_dict[col_name] = deserialized_value
        return item_dict

    def close(self):
        """Closes the database connection."""
        if self.sql_handler:
            self.sql_handler.close()

    def __del__(self):
        # Ensure connection is closed when object is garbage collected,
        # though explicit close() is preferred.
        self.close()


class PersistentDict(PersistentBase, collections.abc.MutableMapping):
    DEFAULT_ID_COLUMN_NAME = "__hakustore_dict_id__"

    def __init__(
        self,
        db_path: str = ":memory:",
        table_name: str = "hakustore_dict",
        id_column_name: str = None,
        db_type: str = "sqlite",
        read_only: bool = False,
        **kwargs,
    ):
        self.id_column_name = id_column_name or self.DEFAULT_ID_COLUMN_NAME
        if not isinstance(self.id_column_name, str):
            raise TypeError("id_column_name must be a string.")
        super().__init__(db_path, table_name, db_type, read_only, **kwargs)

    def _init_db_structure(self):
        # The key of PersistentDict will be serialized to TEXT for the ID column
        id_col_generic_type = "TEXT"

        # Check if table exists
        if not self.sql_handler.table_exists(self.table_name):
            # Create table with the ID column
            cols_to_create = {self.id_column_name: id_col_generic_type}
            self.sql_handler.create_table(
                self.table_name, cols_to_create, primary_key_column=self.id_column_name
            )
            self._columns_cache = {
                self.id_column_name: self.sql_handler.map_generic_type_to_specific(
                    id_col_generic_type
                )
            }
        else:
            # Table exists, ensure ID column is there (though it should be if created by us)
            current_cols = self._get_current_columns(use_cache=False)
            if self.id_column_name not in current_cols:
                # This is a problematic state - table exists but without our required ID column
                # Or ID column has changed. For now, error out.
                # A more robust solution might try to ALTER or migrate.
                raise RuntimeError(
                    f"Table '{self.table_name}' exists but is missing the required ID column '{self.id_column_name}'. "
                    f"Ensure the table schema is compatible or use a different table name."
                )
            # PK constraint cannot be easily added to existing column with ALTER TABLE in SQLite.
            # We assume if column exists, it was created correctly as PK by us.
            self._columns_cache = current_cols

    def __setitem__(self, key, value_dict):
        if self.read_only:
            raise PermissionError("Cannot write to a read-only PersistentDict")
        if not isinstance(value_dict, dict):
            raise TypeError(
                f"Value for PersistentDict must be a dict, got {type(value_dict)}"
            )
        if not all(isinstance(k, str) for k in value_dict.keys()):
            raise TypeError(
                "Keys of the dictionary being stored as a value must all be strings."
            )

        self._ensure_columns(
            value_dict
        )  # Ensure all keys in value_dict exist as columns

        # Prepare data for DB: serialize PersistentDict key for ID column, and all values in value_dict
        serialized_id_key, _ = get_db_type_and_serialized_value(
            key
        )  # Dict key -> TEXT for ID

        db_data = {self.id_column_name: serialized_id_key}
        for k, v_val in value_dict.items():
            s_val, _ = get_db_type_and_serialized_value(v_val)
            db_data[k] = s_val

        # Use handler's method for INSERT OR REPLACE / ON CONFLICT UPDATE
        sql, params = self.sql_handler.get_insert_or_replace_sql(
            self.table_name, db_data, self.id_column_name
        )
        self.sql_handler.execute_query(sql, params, commit=True)

    def __getitem__(self, key):
        serialized_id_key, _ = get_db_type_and_serialized_value(key)
        q_table = self.sql_handler.quote_identifier(self.table_name)
        q_id_col = self.sql_handler.quote_identifier(self.id_column_name)

        # Fetch all columns for the row
        query = f"SELECT * FROM {q_table} WHERE {q_id_col} = ?"

        row_data, description = self.sql_handler.execute_query(
            query, (serialized_id_key,), fetch_one=True, get_cursor_description=True
        )

        if row_data is None:
            raise KeyError(key)

        column_names_from_cursor = [desc[0] for desc in description]
        # Use cached schema types; refresh if needed or assume it's mostly stable for reads
        column_types_from_schema = self._get_current_columns(use_cache=True)

        processed_dict = self._process_row_to_dict(
            row_data, column_names_from_cursor, column_types_from_schema
        )

        # The ID column itself is not part of the returned value_dict
        if self.id_column_name in processed_dict:
            del processed_dict[self.id_column_name]

        return processed_dict

    def __delitem__(self, key):
        if self.read_only:
            raise PermissionError("Cannot delete from a read-only PersistentDict")

        serialized_id_key, _ = get_db_type_and_serialized_value(key)
        q_table = self.sql_handler.quote_identifier(self.table_name)
        q_id_col = self.sql_handler.quote_identifier(self.id_column_name)

        # Check existence first to provide Pythonic KeyError
        check_query = f"SELECT 1 FROM {q_table} WHERE {q_id_col} = ?"
        if (
            self.sql_handler.execute_query(
                check_query, (serialized_id_key,), fetch_one=True
            )
            is None
        ):
            raise KeyError(key)

        del_query = f"DELETE FROM {q_table} WHERE {q_id_col} = ?"
        self.sql_handler.execute_query(del_query, (serialized_id_key,), commit=True)

    def __iter__(self):
        q_table = self.sql_handler.quote_identifier(self.table_name)
        q_id_col = self.sql_handler.quote_identifier(self.id_column_name)
        query = f"SELECT {q_id_col} FROM {q_table}"

        # Get schema for ID column to deserialize correctly
        id_col_type = self._get_current_columns().get(self.id_column_name, "TEXT")

        rows = self.sql_handler.execute_query(query, fetch_all=True)
        for row in rows:
            # row[0] is the serialized ID key
            deserialized_key = deserialize_db_value(row[0], id_col_type)
            # If SQL_NULL_SENTINEL is returned, it's an issue (PK shouldn't be NULL)
            # but deserialize_db_value should handle it returning None if JSON "null"
            if deserialized_key is SQL_NULL_SENTINEL:
                logger.error(
                    f"Primary key {self.id_column_name} is NULL in table {self.table_name}. This should not happen."
                )
                continue  # or raise error
            yield deserialized_key

    def __len__(self):
        q_table = self.sql_handler.quote_identifier(self.table_name)
        query = f"SELECT COUNT(*) FROM {q_table}"
        result = self.sql_handler.execute_query(query, fetch_one=True)
        return result[0] if result else 0

    def __repr__(self):
        return f"PersistentDict({self.db_path}, {self.table_name}, read_only={self.read_only})"

    def __str__(self):
        result_str = "{\n"
        for key, value in self.items():
            result_str += f"  {key}: {value},\n"
        result_str = result_str.rstrip(", ") + "}"
        return f"PersistentDict({result_str})"