import json
from hakustore.logger import logger

SQL_NULL_SENTINEL = (
    object()
)  # Sentinel to distinguish SQL NULL from Python None after deserialization


def get_db_type_and_serialized_value(value):
    """
    Determines the generic DB type for a Python value and serializes if necessary.
    Returns a tuple: (serialized_value, generic_db_type_str).
    Generic DB types: "TEXT", "INTEGER", "REAL", "BLOB".
    """
    if isinstance(value, (list, dict)) or value is None or isinstance(value, bool):
        try:
            return json.dumps(value), "TEXT"
        except TypeError as e:
            logger.error(
                f"Could not JSON serialize value of type {type(value)}: {e}. Storing as string."
            )
            return str(value), "TEXT"  # Fallback for unjsonable complex types
    elif isinstance(value, int):
        return value, "INTEGER"
    elif isinstance(value, float):
        return value, "REAL"
    elif isinstance(value, str):
        return value, "TEXT"
    elif isinstance(value, bytes):
        return value, "BLOB"
    else:
        # Fallback for other types (e.g., custom objects)
        logger.warning(
            f"Value of type {type(value)} is not a basic type or explicitly serializable. "
            f"Attempting JSON serialization. Ensure it's supported or provide a string/bytes representation."
        )
        try:
            return json.dumps(value), "TEXT"
        except TypeError:
            logger.error(
                f"Fallback JSON serialization failed for type {type(value)}. Storing as str()."
            )
            return str(value), "TEXT"


def deserialize_db_value(db_value_from_cursor, column_declared_type: str):
    """
    Deserializes a value fetched from the DB cursor.
    `db_value_from_cursor` is the raw value from the database.
    `column_declared_type` is the type string (e.g., "TEXT", "INTEGER") as known from DB schema.
    Returns the Python value, or SQL_NULL_SENTINEL if db_value_from_cursor was SQL NULL.
    """
    if db_value_from_cursor is None:  # This means SQL NULL
        return SQL_NULL_SENTINEL

    # We primarily rely on JSON for complex types stored in TEXT columns
    if isinstance(db_value_from_cursor, str) and column_declared_type.upper() in (
        "TEXT",
        "VARCHAR",
        "CHAR",
    ):  # Check if it's a text type column
        try:
            # Attempt to parse as JSON.
            # This is for values we explicitly serialized as JSON (lists, dicts, bools, None).
            # e.g. "null", "true", "[1,2]", "{\"a\":1}"
            if db_value_from_cursor.startswith(("[", "{")) or db_value_from_cursor in (
                "null",
                "true",
                "false",
            ):
                return json.loads(db_value_from_cursor)
            # If it doesn't look like our typical JSON strings, or if json.loads fails below,
            # it's treated as a plain string.
        except json.JSONDecodeError:
            # It was a string in the DB that is not valid JSON, return as is.
            return db_value_from_cursor
        # If it was a string but not one of the above patterns, it's a plain string.
        return db_value_from_cursor

    # For non-string types or strings not parsed as JSON, return as is.
    # (e.g., INTEGER, REAL, BLOB from DB are already Python int, float, bytes)
    return db_value_from_cursor
