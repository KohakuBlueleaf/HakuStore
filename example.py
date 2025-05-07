# example.py
import os
import shutil
from hakustore import PersistentDict, logger, DBType

# logger.setLevel("DEBUG")  # Set logger to DEBUG level for detailed output


# Setup: Ensure clean state for examples
DB_DIR = "haku_dbs"
if os.path.exists(DB_DIR):
    shutil.rmtree(DB_DIR)
os.makedirs(DB_DIR, exist_ok=True)

SQLITE_DICT_DB = os.path.join(DB_DIR, "my_persistent_dict.sqlite")
SQLITE_LIST_DB = os.path.join(DB_DIR, "my_persistent_list.sqlite")


def example_persistent_dict(db_path, db_type_str):
    logger.info(f"\n--- Running PersistentDict Example ({db_type_str.upper()}) ---")

    # Create or connect to a persistent dict
    # Custom ID column name
    p_dict = PersistentDict(
        db_path, "my_objects", id_column_name="object_id", db_type=db_type_str
    )

    # Add items (like a Python dict)
    p_dict["user123"] = {"name": "Alice", "age": 30, "city": "New York"}
    p_dict["item456"] = {"type": "gadget", "price": 99.99, "available": True}

    logger.info(f"p_dict after initial adds: {dict(p_dict.items())}")

    # Access items
    logger.info(f"User123 data: {p_dict['user123']}")

    # Update an item - new keys will add columns
    p_dict["user123"] = {
        "name": "Alice Wonderland",
        "age": 31,
        "email": "alice@example.com",
    }
    logger.info(f"User123 updated: {p_dict['user123']}")

    # Add an item with a key (for the ID column) that's not a string
    p_dict[789] = {"description": "Numeric key test", "value": 789}
    logger.info(f"Item with numeric key 789: {p_dict[789]}")

    # Demonstrate missing keys and None values
    p_dict["product789"] = {
        "name": "Widget",
        "color": "Blue",
        "rating": None,
    }  # Explicit None
    p_dict["productABC"] = {
        "name": "Gizmo"
    }  # 'color' and 'rating' will be missing (SQL NULL)

    logger.info(f"Product 789 (with None): {p_dict['product789']}")
    logger.info(f"Product ABC (missing keys): {p_dict['productABC']}")

    # Length
    logger.info(f"Number of items in p_dict: {len(p_dict)}")

    # Iteration
    logger.info("Iterating through p_dict keys:")
    for key in p_dict:
        logger.info(f"  Key: {key}, Value: {p_dict[key]}")

    # Deletion
    del p_dict["item456"]
    logger.info(f"Length after deleting 'item456': {len(p_dict)}")
    assert "item456" not in p_dict

    try:
        _ = p_dict["non_existent_key"]
    except KeyError as e:
        logger.info(f"Correctly caught KeyError: {e}")
    print(p_dict)

    # Test read-only mode
    p_dict.close()  # Close writeable connection

    p_dict_ro = PersistentDict(
        db_path,
        "my_objects",
        id_column_name="object_id",
        db_type=db_type_str,
        read_only=True,
    )
    logger.info(f"Read-only access to user123: {p_dict_ro['user123']}")
    try:
        p_dict_ro["new_key"] = {"data": "test"}
        logger.error("Error: Should not be able to write in read-only mode")
    except PermissionError as e:
        logger.info(
            f"Correctly caught PermissionError for write in read-only mode: {e}"
        )
    p_dict_ro.close()

    # Clean up by closing the original p_dict if it wasn't closed
    # (PersistentBase __del__ handles it, but explicit is good)
    # p_dict.close() # Already closed before read-only test


if __name__ == "__main__":
    # SQLite Examples
    logger.info("====== RUNNING SQLITE EXAMPLES ======")
    example_persistent_dict(SQLITE_DICT_DB, DBType.SQLITE.value)
    logger.info("\n--- All Examples Finished ---")
