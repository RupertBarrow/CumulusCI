import pandas as pd
import pytest

from cumulusci.tasks.bulkdata.select_utils import (
    SelectOperationExecutor,
    SelectStrategy,
    add_limit_offset_to_user_filter,
    annoy_post_process,
    calculate_levenshtein_distance,
    determine_field_types,
    find_closest_record,
    levenshtein_distance,
    replace_empty_strings_with_missing,
    vectorize_records,
)


# Test Cases for standard_generate_query
def test_standard_generate_query_with_default_record_declaration():
    select_operator = SelectOperationExecutor(SelectStrategy.STANDARD)
    sobject = "Account"  # Assuming Account has a declaration in DEFAULT_DECLARATIONS
    limit = 5
    offset = 2
    query, fields = select_operator.select_generate_query(
        sobject=sobject, fields=[], user_filter="", limit=limit, offset=offset
    )

    assert "WHERE" in query  # Ensure WHERE clause is included
    assert f"LIMIT {limit}" in query
    assert f"OFFSET {offset}" in query
    assert fields == ["Id"]


def test_standard_generate_query_without_default_record_declaration():
    select_operator = SelectOperationExecutor(SelectStrategy.STANDARD)
    sobject = "Contact"  # Assuming no declaration for this object
    limit = 3
    offset = None
    query, fields = select_operator.select_generate_query(
        sobject=sobject, fields=[], user_filter="", limit=limit, offset=offset
    )

    assert "WHERE" not in query  # No WHERE clause should be present
    assert f"LIMIT {limit}" in query
    assert "OFFSET" not in query
    assert fields == ["Id"]


def test_standard_generate_query_with_user_filter():
    select_operator = SelectOperationExecutor(SelectStrategy.STANDARD)
    sobject = "Contact"  # Assuming no declaration for this object
    limit = 3
    offset = None
    user_filter = "WHERE Name IN ('Sample Contact')"
    query, fields = select_operator.select_generate_query(
        sobject=sobject, fields=[], user_filter=user_filter, limit=limit, offset=offset
    )

    assert "WHERE" in query
    assert "Sample Contact" in query
    assert "LIMIT" in query
    assert "OFFSET" not in query
    assert fields == ["Id"]


# Test Cases for random generate query
def test_random_generate_query_with_default_record_declaration():
    select_operator = SelectOperationExecutor(SelectStrategy.RANDOM)
    sobject = "Account"  # Assuming Account has a declaration in DEFAULT_DECLARATIONS
    limit = 5
    offset = 2
    query, fields = select_operator.select_generate_query(
        sobject=sobject, fields=[], user_filter="", limit=limit, offset=offset
    )

    assert "WHERE" in query  # Ensure WHERE clause is included
    assert f"LIMIT {limit}" in query
    assert f"OFFSET {offset}" in query
    assert fields == ["Id"]


def test_random_generate_query_without_default_record_declaration():
    select_operator = SelectOperationExecutor(SelectStrategy.RANDOM)
    sobject = "Contact"  # Assuming no declaration for this object
    limit = 3
    offset = None
    query, fields = select_operator.select_generate_query(
        sobject=sobject, fields=[], user_filter="", limit=limit, offset=offset
    )

    assert "WHERE" not in query  # No WHERE clause should be present
    assert f"LIMIT {limit}" in query
    assert "OFFSET" not in query
    assert fields == ["Id"]


# Test Cases for standard_post_process
def test_standard_post_process_with_records():
    select_operator = SelectOperationExecutor(SelectStrategy.STANDARD)
    records = [["001"], ["002"], ["003"]]
    num_records = 3
    sobject = "Contact"
    selected_records, error_message = select_operator.select_post_process(
        None, records, num_records, sobject, weights=[]
    )

    assert error_message is None
    assert len(selected_records) == num_records
    assert all(record["success"] for record in selected_records)
    assert all(record["created"] is False for record in selected_records)
    assert all(record["id"] in ["001", "002", "003"] for record in selected_records)


def test_standard_post_process_with_fewer_records():
    select_operator = SelectOperationExecutor(SelectStrategy.STANDARD)
    records = [["001"]]
    num_records = 3
    sobject = "Opportunity"
    selected_records, error_message = select_operator.select_post_process(
        None, records, num_records, sobject, weights=[]
    )

    assert error_message is None
    assert len(selected_records) == num_records
    assert all(record["success"] for record in selected_records)
    assert all(record["created"] is False for record in selected_records)
    # Check if records are repeated to match num_records
    assert selected_records.count({"id": "001", "success": True, "created": False}) == 3


def test_standard_post_process_with_no_records():
    select_operator = SelectOperationExecutor(SelectStrategy.STANDARD)
    records = []
    num_records = 2
    sobject = "Lead"
    selected_records, error_message = select_operator.select_post_process(
        None, records, num_records, sobject, weights=[]
    )

    assert selected_records == []
    assert error_message == f"No records found for {sobject} in the target org."


# Test cases for Random Post Process
def test_random_post_process_with_records():
    select_operator = SelectOperationExecutor(SelectStrategy.RANDOM)
    records = [["001"], ["002"], ["003"]]
    num_records = 3
    sobject = "Contact"
    selected_records, error_message = select_operator.select_post_process(
        None, records, num_records, sobject, weights=[]
    )

    assert error_message is None
    assert len(selected_records) == num_records
    assert all(record["success"] for record in selected_records)
    assert all(record["created"] is False for record in selected_records)


def test_random_post_process_with_no_records():
    select_operator = SelectOperationExecutor(SelectStrategy.RANDOM)
    records = []
    num_records = 2
    sobject = "Lead"
    selected_records, error_message = select_operator.select_post_process(
        None, records, num_records, sobject, weights=[]
    )

    assert selected_records == []
    assert error_message == f"No records found for {sobject} in the target org."


# Test Cases for Similarity Generate Query
def test_similarity_generate_query_with_default_record_declaration():
    select_operator = SelectOperationExecutor(SelectStrategy.SIMILARITY)
    sobject = "Account"  # Assuming Account has a declaration in DEFAULT_DECLARATIONS
    limit = 5
    offset = 2
    query, fields = select_operator.select_generate_query(
        sobject, ["Name"], [], limit, offset
    )

    assert "WHERE" in query  # Ensure WHERE clause is included
    assert fields == ["Id", "Name"]
    assert f"LIMIT {limit}" in query
    assert f"OFFSET {offset}" in query


def test_similarity_generate_query_without_default_record_declaration():
    select_operator = SelectOperationExecutor(SelectStrategy.SIMILARITY)
    sobject = "Contact"  # Assuming no declaration for this object
    limit = 3
    offset = None
    query, fields = select_operator.select_generate_query(
        sobject, ["Name"], [], limit, offset
    )

    assert "WHERE" not in query  # No WHERE clause should be present
    assert fields == ["Id", "Name"]
    assert f"LIMIT {limit}" in query
    assert "OFFSET" not in query


def test_similarity_generate_query_with_nested_fields():
    select_operator = SelectOperationExecutor(SelectStrategy.SIMILARITY)
    sobject = "Event"  # Assuming no declaration for this object
    limit = 3
    offset = None
    fields = [
        "Subject",
        "Who.Contact.Name",
        "Who.Contact.Email",
        "Who.Lead.Name",
        "Who.Lead.Company",
    ]
    query, query_fields = select_operator.select_generate_query(
        sobject, fields, [], limit, offset
    )

    assert "WHERE" not in query  # No WHERE clause should be present
    assert query_fields == [
        "Id",
        "Subject",
        "Who.Contact.Name",
        "Who.Contact.Email",
        "Who.Lead.Name",
        "Who.Lead.Company",
    ]
    assert f"LIMIT {limit}" in query
    assert "TYPEOF Who" in query
    assert "WHEN Contact" in query
    assert "WHEN Lead" in query
    assert "OFFSET" not in query


def test_random_generate_query_with_user_filter():
    select_operator = SelectOperationExecutor(SelectStrategy.SIMILARITY)
    sobject = "Contact"  # Assuming no declaration for this object
    limit = 3
    offset = None
    user_filter = "WHERE Name IN ('Sample Contact')"
    query, fields = select_operator.select_generate_query(
        sobject=sobject,
        fields=["Name"],
        user_filter=user_filter,
        limit=limit,
        offset=offset,
    )

    assert "WHERE" in query
    assert "Sample Contact" in query
    assert "LIMIT" in query
    assert "OFFSET" not in query
    assert fields == ["Id", "Name"]


def test_levenshtein_distance():
    assert levenshtein_distance("kitten", "kitten") == 0  # Identical strings
    assert levenshtein_distance("kitten", "sitten") == 1  # One substitution
    assert levenshtein_distance("kitten", "kitte") == 1  # One deletion
    assert levenshtein_distance("kitten", "sittin") == 2  # Two substitutions
    assert levenshtein_distance("kitten", "dog") == 6  # Completely different strings
    assert levenshtein_distance("kitten", "") == 6  # One string is empty
    assert levenshtein_distance("", "") == 0  # Both strings are empty
    assert levenshtein_distance("Kitten", "kitten") == 1  # Case sensitivity
    assert levenshtein_distance("kit ten", "kitten") == 1  # Strings with spaces
    assert (
        levenshtein_distance("levenshtein", "meilenstein") == 4
    )  # Longer strings with multiple differences


def test_find_closest_record_different_weights():
    load_record = ["hello", "world"]
    query_records = [
        ["record1", "hello", "word"],  # Levenshtein distance = 1
        ["record2", "hullo", "word"],  # Levenshtein distance = 1
        ["record3", "hello", "word"],  # Levenshtein distance = 1
    ]
    weights = [2.0, 0.5]

    # With different weights, the first field will have more impact
    closest_record = find_closest_record(load_record, query_records, weights)
    assert closest_record == [
        "record1",
        "hello",
        "word",
    ], "The closest record should be 'record1'."


def test_find_closest_record_basic():
    load_record = ["hello", "world"]
    query_records = [
        ["record1", "hello", "word"],  # Levenshtein distance = 1
        ["record2", "hullo", "word"],  # Levenshtein distance = 1
        ["record3", "hello", "word"],  # Levenshtein distance = 1
    ]
    weights = [1.0, 1.0]

    closest_record = find_closest_record(load_record, query_records, weights)
    assert closest_record == [
        "record1",
        "hello",
        "word",
    ], "The closest record should be 'record1'."


def test_find_closest_record_multiple_matches():
    load_record = ["cat", "dog"]
    query_records = [
        ["record1", "bat", "dog"],  # Levenshtein distance = 1
        ["record2", "cat", "dog"],  # Levenshtein distance = 0
        ["record3", "dog", "cat"],  # Levenshtein distance = 3
    ]
    weights = [1.0, 1.0]

    closest_record = find_closest_record(load_record, query_records, weights)
    assert closest_record == [
        "record2",
        "cat",
        "dog",
    ], "The closest record should be 'record2'."


def test_similarity_post_process_with_records():
    select_operator = SelectOperationExecutor(SelectStrategy.SIMILARITY)
    num_records = 1
    sobject = "Contact"
    load_records = [["Tom Cruise", "62", "Actor"]]
    query_records = [
        ["001", "Tom Hanks", "62", "Actor"],
        ["002", "Tom Cruise", "63", "Actor"],  # Slight difference
        ["003", "Jennifer Aniston", "30", "Actress"],
    ]

    weights = [1.0, 1.0, 1.0]  # Adjust weights to match your data structure

    selected_records, error_message = select_operator.select_post_process(
        load_records, query_records, num_records, sobject, weights
    )

    # selected_records, error_message = select_operator.select_post_process(
    #     load_records, query_records, num_records, sobject
    # )

    assert error_message is None
    assert len(selected_records) == num_records
    assert all(record["success"] for record in selected_records)
    assert all(record["created"] is False for record in selected_records)
    assert all(record["id"] in ["002"] for record in selected_records)


def test_similarity_post_process_with_no_records():
    select_operator = SelectOperationExecutor(SelectStrategy.SIMILARITY)
    records = []
    num_records = 2
    sobject = "Lead"
    selected_records, error_message = select_operator.select_post_process(
        None, records, num_records, sobject, weights=[1, 1, 1]
    )

    assert selected_records == []
    assert error_message == f"No records found for {sobject} in the target org."


def test_calculate_levenshtein_distance_basic():
    record1 = ["hello", "world"]
    record2 = ["hullo", "word"]
    weights = [1.0, 1.0]

    # Expected distance based on simple Levenshtein distances
    # Levenshtein("hello", "hullo") = 1, Levenshtein("world", "word") = 1
    expected_distance = (1 * 1.0 + 1 * 1.0) / 2  # Averaged over two fields

    result = calculate_levenshtein_distance(record1, record2, weights)
    assert result == pytest.approx(
        expected_distance
    ), "Basic distance calculation failed."

    # Empty fields
    record1 = ["hello", ""]
    record2 = ["hullo", ""]
    weights = [1.0, 1.0]

    # Expected distance based on simple Levenshtein distances
    # Levenshtein("hello", "hullo") = 1, Levenshtein("", "") = 0
    expected_distance = (1 * 1.0 + 0 * 1.0) / 2  # Averaged over two fields

    result = calculate_levenshtein_distance(record1, record2, weights)
    assert result == pytest.approx(
        expected_distance
    ), "Basic distance calculation with empty fields failed."

    # Partial empty fields
    record1 = ["hello", "world"]
    record2 = ["hullo", ""]
    weights = [1.0, 1.0]

    # Expected distance based on simple Levenshtein distances
    # Levenshtein("hello", "hullo") = 1, Levenshtein("world", "") = 5
    expected_distance = (1 * 1.0 + 5 * 0.05 * 1.0) / 2  # Averaged over two fields

    result = calculate_levenshtein_distance(record1, record2, weights)
    assert result == pytest.approx(
        expected_distance
    ), "Basic distance calculation with partial empty fields failed."


def test_calculate_levenshtein_distance_weighted():
    record1 = ["cat", "dog"]
    record2 = ["bat", "fog"]
    weights = [2.0, 0.5]

    # Levenshtein("cat", "bat") = 1, Levenshtein("dog", "fog") = 1
    expected_distance = (1 * 2.0 + 1 * 0.5) / 2  # Weighted average over two fields

    result = calculate_levenshtein_distance(record1, record2, weights)
    assert result == pytest.approx(
        expected_distance
    ), "Weighted distance calculation failed."


def test_calculate_levenshtein_distance_records_length_doesnt_match():
    record1 = ["cat", "dog", "cow"]
    record2 = ["bat", "fog"]
    weights = [2.0, 0.5]

    with pytest.raises(ValueError) as e:
        calculate_levenshtein_distance(record1, record2, weights)
    assert "Records must have the same number of fields." in str(e.value)


def test_calculate_levenshtein_distance_weights_length_doesnt_match():
    record1 = ["cat", "dog"]
    record2 = ["bat", "fog"]
    weights = [2.0, 0.5, 3.0]

    with pytest.raises(ValueError) as e:
        calculate_levenshtein_distance(record1, record2, weights)
    assert "Records must be same size as fields (weights)." in str(e.value)


def test_replace_empty_strings_with_missing():
    # Case 1: Normal case with some empty strings
    records = [
        ["Alice", "", "New York"],
        ["Bob", "Engineer", ""],
        ["", "Teacher", "Chicago"],
    ]
    expected = [
        ["Alice", "missing", "New York"],
        ["Bob", "Engineer", "missing"],
        ["missing", "Teacher", "Chicago"],
    ]
    assert replace_empty_strings_with_missing(records) == expected

    # Case 2: No empty strings, so the output should be the same as input
    records = [["Alice", "Manager", "New York"], ["Bob", "Engineer", "San Francisco"]]
    expected = [["Alice", "Manager", "New York"], ["Bob", "Engineer", "San Francisco"]]
    assert replace_empty_strings_with_missing(records) == expected

    # Case 3: List with all empty strings
    records = [["", "", ""], ["", "", ""]]
    expected = [["missing", "missing", "missing"], ["missing", "missing", "missing"]]
    assert replace_empty_strings_with_missing(records) == expected

    # Case 4: Empty list (should return an empty list)
    records = []
    expected = []
    assert replace_empty_strings_with_missing(records) == expected

    # Case 5: List with some empty sublists
    records = [[], ["Alice", ""], []]
    expected = [[], ["Alice", "missing"], []]
    assert replace_empty_strings_with_missing(records) == expected


def test_all_numeric_columns():
    df = pd.DataFrame({"A": [1, 2, 3], "B": [4.5, 5.5, 6.5]})
    weights = [0.1, 0.2]
    expected_output = (
        ["A", "B"],  # numerical_features
        [],  # boolean_features
        [],  # categorical_features
        [0.1, 0.2],  # numerical_weights
        [],  # boolean_weights
        [],  # categorical_weights
    )
    assert determine_field_types(df, weights) == expected_output


def test_all_boolean_columns():
    df = pd.DataFrame({"A": ["true", "false", "true"], "B": ["false", "true", "false"]})
    weights = [0.3, 0.4]
    expected_output = (
        [],  # numerical_features
        ["A", "B"],  # boolean_features
        [],  # categorical_features
        [],  # numerical_weights
        [0.3, 0.4],  # boolean_weights
        [],  # categorical_weights
    )
    assert determine_field_types(df, weights) == expected_output


def test_all_categorical_columns():
    df = pd.DataFrame(
        {"A": ["apple", "banana", "cherry"], "B": ["dog", "cat", "mouse"]}
    )
    weights = [0.5, 0.6]
    expected_output = (
        [],  # numerical_features
        [],  # boolean_features
        ["A", "B"],  # categorical_features
        [],  # numerical_weights
        [],  # boolean_weights
        [0.5, 0.6],  # categorical_weights
    )
    assert determine_field_types(df, weights) == expected_output


def test_mixed_types():
    df = pd.DataFrame(
        {
            "A": [1, 2, 3],
            "B": ["true", "false", "true"],
            "C": ["apple", "banana", "cherry"],
        }
    )
    weights = [0.7, 0.8, 0.9]
    expected_output = (
        ["A"],  # numerical_features
        ["B"],  # boolean_features
        ["C"],  # categorical_features
        [0.7],  # numerical_weights
        [0.8],  # boolean_weights
        [0.9],  # categorical_weights
    )
    assert determine_field_types(df, weights) == expected_output


def test_vectorize_records_mixed_numerical_boolean_categorical():
    # Test data with mixed types: numerical and categorical only
    db_records = [["1.0", "true", "apple"], ["2.0", "false", "banana"]]
    query_records = [["1.5", "true", "apple"], ["2.5", "false", "cherry"]]
    weights = [1.0, 1.0, 1.0]  # Equal weights for numerical and categorical columns
    hash_features = 4  # Number of hashing vectorizer features for categorical columns

    final_db_vectors, final_query_vectors = vectorize_records(
        db_records, query_records, hash_features, weights
    )

    # Check the shape of the output vectors
    assert final_db_vectors.shape[0] == len(db_records), "DB vectors row count mismatch"
    assert final_query_vectors.shape[0] == len(
        query_records
    ), "Query vectors row count mismatch"

    # Expected dimensions: numerical (1) + categorical hashed features (4)
    expected_feature_count = 2 + hash_features
    assert (
        final_db_vectors.shape[1] == expected_feature_count
    ), "DB vectors column count mismatch"
    assert (
        final_query_vectors.shape[1] == expected_feature_count
    ), "Query vectors column count mismatch"


def test_annoy_post_process():
    # Test data
    load_records = [["Alice", "Engineer"], ["Bob", "Doctor"]]
    query_records = [["q1", "Alice", "Engineer"], ["q2", "Charlie", "Artist"]]
    weights = [1.0, 1.0, 1.0]  # Example weights

    closest_records, error = annoy_post_process(load_records, query_records, weights)

    # Assert the closest records
    assert (
        len(closest_records) == 2
    )  # We expect two results (one for each query record)
    assert (
        closest_records[0]["id"] == "q1"
    )  # The first query record should match the first load record

    # No errors expected
    assert error is None


def test_single_record_match_annoy_post_process():
    # Mock data where only the first query record matches the first load record
    load_records = [["Alice", "Engineer"], ["Bob", "Doctor"]]
    query_records = [["q1", "Alice", "Engineer"]]
    weights = [1.0, 1.0, 1.0]

    closest_records, error = annoy_post_process(load_records, query_records, weights)

    # Both the load records should be matched with the only query record we have
    assert len(closest_records) == 2
    assert closest_records[0]["id"] == "q1"
    assert error is None


@pytest.mark.parametrize(
    "filter_clause, limit_clause, offset_clause, expected",
    [
        # Test: No existing LIMIT/OFFSET and no new clauses
        ("SELECT * FROM users", None, None, " SELECT * FROM users"),
        # Test: Existing LIMIT and no new limit provided
        ("SELECT * FROM users LIMIT 100", None, None, "SELECT * FROM users LIMIT 100"),
        # Test: Existing OFFSET and no new offset provided
        ("SELECT * FROM users OFFSET 20", None, None, "SELECT * FROM users OFFSET 20"),
        # Test: Existing LIMIT/OFFSET and new clauses provided
        (
            "SELECT * FROM users LIMIT 100 OFFSET 20",
            50,
            10,
            "SELECT * FROM users LIMIT 50 OFFSET 30",
        ),
        # Test: Existing LIMIT, new limit larger than existing (should keep the smaller one)
        ("SELECT * FROM users LIMIT 100", 150, None, "SELECT * FROM users LIMIT 100"),
        # Test: New limit smaller than existing (should use the new one)
        ("SELECT * FROM users LIMIT 100", 50, None, "SELECT * FROM users LIMIT 50"),
        # Test: Existing OFFSET, adding a new offset (should sum the offsets)
        ("SELECT * FROM users OFFSET 20", None, 30, "SELECT * FROM users OFFSET 50"),
        # Test: Existing LIMIT/OFFSET and new values set to None
        (
            "SELECT * FROM users LIMIT 100 OFFSET 20",
            None,
            None,
            "SELECT * FROM users LIMIT 100 OFFSET 20",
        ),
        # Test: Removing existing LIMIT and adding a new one
        ("SELECT * FROM users LIMIT 200", 50, None, "SELECT * FROM users LIMIT 50"),
        # Test: Removing existing OFFSET and adding a new one
        ("SELECT * FROM users OFFSET 40", None, 20, "SELECT * FROM users OFFSET 60"),
        # Edge case: Filter clause with mixed cases
        (
            "SELECT * FROM users LiMiT 100 oFfSeT 20",
            50,
            10,
            "SELECT * FROM users LIMIT 50 OFFSET 30",
        ),
        # Test: Filter clause with trailing/leading spaces
        (
            "   SELECT * FROM users   LIMIT 100   OFFSET 20   ",
            50,
            10,
            "SELECT * FROM users LIMIT 50 OFFSET 30",
        ),
    ],
)
def test_add_limit_offset_to_user_filter(
    filter_clause, limit_clause, offset_clause, expected
):
    result = add_limit_offset_to_user_filter(filter_clause, limit_clause, offset_clause)
    assert result.strip() == expected.strip()
