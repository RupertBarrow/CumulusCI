import csv
import io
import json
import os
import pathlib
import re
import tempfile
import time
from abc import ABCMeta, abstractmethod
from contextlib import contextmanager
from itertools import tee
from typing import Any, Dict, List, NamedTuple, Optional, Union

import requests
import salesforce_bulk

from cumulusci.core.enums import StrEnum
from cumulusci.core.exceptions import BulkDataException, SOQLQueryException
from cumulusci.core.utils import process_bool_arg
from cumulusci.tasks.bulkdata.select_utils import (
    SelectOperationExecutor,
    SelectStrategy,
)
from cumulusci.tasks.bulkdata.utils import DataApi, iterate_in_chunks
from cumulusci.utils.classutils import namedtuple_as_simple_dict
from cumulusci.utils.xml import lxml_parse_string

DEFAULT_BULK_BATCH_SIZE = 10_000
DEFAULT_REST_BATCH_SIZE = 200
MAX_REST_BATCH_SIZE = 200
csv.field_size_limit(2**27)  # 128 MB


class DataOperationType(StrEnum):
    """Enum defining the API data operation requested."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    HARD_DELETE = "hardDelete"
    QUERY = "query"
    UPSERT = "upsert"
    ETL_UPSERT = "etl_upsert"
    SMART_UPSERT = "smart_upsert"  # currently undocumented
    SELECT = "select"


class DataOperationStatus(StrEnum):
    """Enum defining outcome values for a data operation."""

    SUCCESS = "Success"
    ROW_FAILURE = "Row failure"
    JOB_FAILURE = "Job failure"
    IN_PROGRESS = "In progress"
    ABORTED = "Aborted"


class DataOperationResult(NamedTuple):
    id: str
    success: bool
    error: str
    created: Optional[bool] = None


class DataOperationJobResult(NamedTuple):
    status: DataOperationStatus
    job_errors: List[str]
    records_processed: int
    total_row_errors: int = 0

    def simplify(self):
        return namedtuple_as_simple_dict(self)


@contextmanager
def download_file(uri, bulk_api, *, chunk_size=8192):
    """Download the Bulk API result file for a single batch,
    and remove it when the context manager exits."""
    try:
        (handle, path) = tempfile.mkstemp(text=False)
        resp = requests.get(uri, headers=bulk_api.headers(), stream=True)
        resp.raise_for_status()
        f = os.fdopen(handle, "wb")
        for chunk in resp.iter_content(chunk_size=chunk_size):  # VCR needs a chunk_size
            # specific chunk_size seems to make no measurable perf difference
            f.write(chunk)

        f.close()
        with open(path, "r", newline="", encoding="utf-8") as f:
            yield f
    finally:
        pathlib.Path(path).unlink()


class BulkJobMixin:
    """Provides mixin utilities for classes that manage Bulk API jobs."""

    def _job_state_from_batches(self, job_id):
        """Query for batches under job_id and return overall status
        inferred from batch-level status values."""
        uri = f"{self.bulk.endpoint}/job/{job_id}/batch"
        response = requests.get(uri, headers=self.bulk.headers())
        response.raise_for_status()
        return self._parse_job_state(response.content)

    def _parse_job_state(self, xml: str):
        """Parse the Bulk API return value and generate a summary status record for the job."""
        tree = lxml_parse_string(xml)
        statuses = [el.text for el in tree.iterfind(".//{%s}state" % self.bulk.jobNS)]
        state_messages = [
            el.text for el in tree.iterfind(".//{%s}stateMessage" % self.bulk.jobNS)
        ]

        # Get how many total records failed across all the batches.
        failures = tree.findall(".//{%s}numberRecordsFailed" % self.bulk.jobNS)
        record_failure_count = sum([int(failure.text) for failure in (failures or [])])

        # Get how many total records processed across all the batches.
        processed = tree.findall(".//{%s}numberRecordsProcessed" % self.bulk.jobNS)
        records_processed_count = sum(
            [int(processed.text) for processed in (processed or [])]
        )
        # FIXME: "Not Processed" to be expected for original batch with PK Chunking Query
        # PK Chunking is not currently supported.
        if "Not Processed" in statuses:
            return DataOperationJobResult(
                DataOperationStatus.ABORTED,
                [],
                records_processed_count,
                record_failure_count,
            )
        elif "InProgress" in statuses or "Queued" in statuses:
            return DataOperationJobResult(
                DataOperationStatus.IN_PROGRESS,
                [],
                records_processed_count,
                record_failure_count,
            )
        elif "Failed" in statuses:
            return DataOperationJobResult(
                DataOperationStatus.JOB_FAILURE,
                state_messages,
                records_processed_count,
                record_failure_count,
            )

        # All the records submitted in this job failed.
        if record_failure_count:
            return DataOperationJobResult(
                DataOperationStatus.ROW_FAILURE,
                [],
                records_processed_count,
                record_failure_count,
            )

        return DataOperationJobResult(
            DataOperationStatus.SUCCESS,
            [],
            records_processed_count,
            record_failure_count,
        )

    def _wait_for_job(self, job_id):
        """Wait for the given job to enter a completed state (success or failure)."""
        while True:
            job_status = self.bulk.job_status(job_id)
            self.logger.info(
                f"Waiting for job {job_id} ({job_status['numberBatchesCompleted']}/{job_status['numberBatchesTotal']} batches complete)"
            )
            result = self._job_state_from_batches(job_id)
            if result.status is not DataOperationStatus.IN_PROGRESS:
                break

            time.sleep(10)
        plural_errors = "Errors" if result.total_row_errors != 1 else "Error"
        errors = (
            f": {result.total_row_errors} {plural_errors}"
            if result.total_row_errors
            else ""
        )
        self.logger.info(
            f"Job {job_id} finished with result: {result.status.value}{errors}"
        )
        if result.status is DataOperationStatus.JOB_FAILURE:
            for state_message in result.job_errors:
                self.logger.error(f"Batch failure message: {state_message}")

        return result


class BaseDataOperation(metaclass=ABCMeta):
    """Abstract base class for all data operations (queries and DML)."""

    def __init__(self, *, sobject, operation, api_options, context):
        self.sobject = sobject
        self.operation = operation
        self.api_options = api_options
        self.context = context
        self.bulk = context.bulk
        self.sf = context.sf
        self.logger = context.logger
        self.job_result = None


class BaseQueryOperation(BaseDataOperation, metaclass=ABCMeta):
    """Abstract base class for query operations in all APIs."""

    def __init__(self, *, sobject, api_options, context, query):
        super().__init__(
            sobject=sobject,
            operation=DataOperationType.QUERY,
            api_options=api_options,
            context=context,
        )
        self.soql = query

    def __enter__(self):
        self.query()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    @abstractmethod
    def query(self):
        """Execute requested query and block until results are available."""
        pass

    @abstractmethod
    def get_results(self):
        """Return a generator of rows from the query."""
        pass


class BulkApiQueryOperation(BaseQueryOperation, BulkJobMixin):
    """Operation class for Bulk API query jobs."""

    def query(self):
        self.job_id = self.bulk.create_query_job(self.sobject, contentType="CSV")
        self.logger.info(f"Created Bulk API query job {self.job_id}")
        self.batch_id = self.bulk.query(self.job_id, self.soql)

        self.job_result = self._wait_for_job(self.job_id)
        self.bulk.close_job(self.job_id)

    def get_results(self):
        # FIXME: For PK Chunking, need to get new batch Ids
        # and retrieve their results. Original batch will not be processed.

        result_ids = self.bulk.get_query_batch_result_ids(
            self.batch_id, job_id=self.job_id
        )
        for result_id in result_ids:
            uri = f"{self.bulk.endpoint}/job/{self.job_id}/batch/{self.batch_id}/result/{result_id}"

            with download_file(uri, self.bulk) as f:
                reader = csv.reader(f)
                self.headers = next(reader)
                if "Records not found for this query" in self.headers:
                    return

                yield from reader


class RestApiQueryOperation(BaseQueryOperation):
    """Operation class for REST API query jobs."""

    def __init__(self, *, sobject, fields, api_options, context, query):
        super().__init__(
            sobject=sobject, api_options=api_options, context=context, query=query
        )
        self.fields = fields

    def query(self):
        self.response = self.sf.query(self.soql)
        self.job_result = DataOperationJobResult(
            DataOperationStatus.SUCCESS, [], self.response["totalSize"], 0
        )

    def get_results(self):
        def convert(rec):
            return [str(rec[f]) if rec[f] is not None else "" for f in self.fields]

        while True:
            yield from (convert(rec) for rec in self.response["records"])
            if not self.response["done"]:
                self.response = self.sf.query_more(
                    self.response["nextRecordsUrl"], identifier_is_url=True
                )
            else:
                return


class BaseDmlOperation(BaseDataOperation, metaclass=ABCMeta):
    """Abstract base class for DML operations in all APIs."""

    def __init__(self, *, sobject, operation, api_options, context, fields):
        super().__init__(
            sobject=sobject,
            operation=operation,
            api_options=api_options,
            context=context,
        )
        self.fields = fields

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.end()

    def start(self):
        """Perform any required setup, such as job initialization, for the operation."""
        pass

    @abstractmethod
    def get_prev_record_values(self, records):
        """Get the previous records values in case of UPSERT and UPDATE to prepare for rollback"""
        pass

    @abstractmethod
    def select_records(self, records):
        """Perform the requested DML operation on the supplied row iterator."""
        pass

    @abstractmethod
    def load_records(self, records):
        """Perform the requested DML operation on the supplied row iterator."""
        pass

    def end(self):
        """Perform any required teardown for the operation before results are returned."""
        pass

    @abstractmethod
    def get_results(self):
        """Return a generator of DataOperationResult objects."""
        pass


class BulkApiDmlOperation(BaseDmlOperation, BulkJobMixin):
    """Operation class for all DML operations run using the Bulk API."""

    def __init__(
        self,
        *,
        sobject,
        operation,
        api_options,
        context,
        fields,
        selection_strategy=SelectStrategy.STANDARD,
        selection_filter=None,
    ):
        super().__init__(
            sobject=sobject,
            operation=operation,
            api_options=api_options,
            context=context,
            fields=fields,
        )
        self.api_options = api_options.copy()
        self.api_options["batch_size"] = (
            self.api_options.get("batch_size") or DEFAULT_BULK_BATCH_SIZE
        )
        self.csv_buff = io.StringIO(newline="")
        self.csv_writer = csv.writer(self.csv_buff, quoting=csv.QUOTE_ALL)

        self.select_operation_executor = SelectOperationExecutor(selection_strategy)
        self.selection_filter = selection_filter

    def start(self):
        self.job_id = self.bulk.create_job(
            self.sobject,
            self.operation.value,
            contentType="CSV",
            concurrency=self.api_options.get("bulk_mode", "Parallel"),
            external_id_name=self.api_options.get("update_key"),
        )

    def end(self):
        self.bulk.close_job(self.job_id)
        if not self.job_result:
            self.job_result = self._wait_for_job(self.job_id)

    def get_prev_record_values(self, records):
        """Get the previous values of the records based on the update key
        to ensure rollback can be performed"""
        # Function to be called only for UPSERT and UPDATE
        assert self.operation in [DataOperationType.UPSERT, DataOperationType.UPDATE]

        self.logger.info(f"Retrieving Previous Record Values of {self.sobject}")
        prev_record_values = []
        relevant_fields = set(self.fields + ["Id"])

        # Set update key
        update_key = (
            self.api_options.get("update_key")
            if self.operation == DataOperationType.UPSERT
            else "Id"
        )

        for count, batch in enumerate(
            self._batch(records, self.api_options["batch_size"])
        ):
            self.context.logger.info(f"Querying batch {count + 1}")

            # Extract update key values from the batch
            update_key_values = [
                rec[update_key]
                for rec in csv.DictReader([line.decode("utf-8") for line in batch])
            ]

            # Construct the SOQL query
            query_fields = ", ".join(relevant_fields)
            query_values = ", ".join(f"'{value}'" for value in update_key_values)
            query = f"SELECT {query_fields} FROM {self.sobject} WHERE {update_key} IN ({query_values})"

            # Execute the query using Bulk API
            job_id = self.bulk.create_query_job(self.sobject, contentType="JSON")
            batch_id = self.bulk.query(job_id, query)
            self.bulk.wait_for_batch(job_id, batch_id)
            self.bulk.close_job(job_id)
            results = self.bulk.get_all_results_for_query_batch(batch_id)

            # Extract relevant fields from results and append to the respective lists
            for result in results:
                result = json.load(salesforce_bulk.util.IteratorBytesIO(result))
                prev_record_values.extend(
                    [[res[key] for key in relevant_fields] for res in result]
                )

        self.logger.info("Done")
        return prev_record_values, tuple(relevant_fields)

    def load_records(self, records):
        self.batch_ids = []

        batch_size = self.api_options["batch_size"]
        for count, csv_batch in enumerate(self._batch(records, batch_size)):
            self.context.logger.info(f"Uploading batch {count + 1}")
            self.batch_ids.append(self.bulk.post_batch(self.job_id, iter(csv_batch)))

    def select_records(self, records):
        """Executes a SOQL query to select records and adds them to results"""

        self.select_results = []  # Store selected records
        query_records = []
        # Create a copy of the generator using tee
        records, records_copy = tee(records)
        # Count total number of records to fetch using the copy
        total_num_records = sum(1 for _ in records_copy)

        # Process in batches based on batch_size from api_options
        for offset in range(
            0, total_num_records, self.api_options.get("batch_size", 500)
        ):
            # Calculate number of records to fetch in this batch
            num_records = min(
                self.api_options.get("batch_size", 500), total_num_records - offset
            )

            # Generate and execute SOQL query
            # (not passing offset as it is not supported in Bulk)
            (
                select_query,
                query_fields,
            ) = self.select_operation_executor.select_generate_query(
                sobject=self.sobject, fields=self.fields, limit=num_records, offset=None
            )
            if self.selection_filter:
                # Generate user filter query if selection_filter is present (offset clause not supported)
                user_query = generate_user_filter_query(
                    filter_clause=self.selection_filter,
                    sobject=self.sobject,
                    fields=["Id"],
                    limit_clause=num_records,
                    offset_clause=None,
                )
                # Execute the user query using Bulk API
                user_query_executor = get_query_operation(
                    sobject=self.sobject,
                    fields=["Id"],
                    api_options=self.api_options,
                    context=self,
                    query=user_query,
                    api=DataApi.BULK,
                )
                user_query_executor.query()
                user_query_records = user_query_executor.get_results()

                # Find intersection based on 'Id'
                user_query_ids = (
                    list(record[0] for record in user_query_records)
                    if user_query_records
                    else []
                )

            # Execute the main select query using Bulk API
            select_query_records = self._execute_select_query(
                select_query=select_query, query_fields=query_fields
            )

            # If user_query_ids exist, filter select_query_records based on the intersection of Ids
            if self.selection_filter:
                # Create a dictionary to map IDs to their corresponding records
                id_to_record_map = {
                    record[query_fields.index("Id")]: record
                    for record in select_query_records
                }
                # Extend query_records in the order of user_query_ids
                query_records.extend(
                    record
                    for id in user_query_ids
                    if (record := id_to_record_map.get(id)) is not None
                )
            else:
                query_records.extend(select_query_records)

        # Post-process the query results
        (
            selected_records,
            error_message,
        ) = self.select_operation_executor.select_post_process(
            load_records=records,
            query_records=query_records,
            num_records=num_records,
            sobject=self.sobject,
        )
        if not error_message:
            self.select_results.extend(selected_records)

        # Update job result based on selection outcome
        self.job_result = DataOperationJobResult(
            status=DataOperationStatus.SUCCESS
            if len(self.select_results)
            else DataOperationStatus.JOB_FAILURE,
            job_errors=[error_message] if error_message else [],
            records_processed=len(self.select_results),
            total_row_errors=0,
        )

    def _execute_select_query(self, select_query: str, query_fields: List[str]):
        """Executes the select Bulk API query and retrieves the results."""
        self.batch_id = self.bulk.query(self.job_id, select_query)
        self._wait_for_job(self.job_id)
        result_ids = self.bulk.get_query_batch_result_ids(
            self.batch_id, job_id=self.job_id
        )
        select_query_records = []
        for result_id in result_ids:
            uri = f"{self.bulk.endpoint}/job/{self.job_id}/batch/{self.batch_id}/result/{result_id}"
            with download_file(uri, self.bulk) as f:
                reader = csv.reader(f)
                self.headers = next(reader)
                if "Records not found for this query" in self.headers:
                    break
                for row in reader:
                    select_query_records.append(row[: len(query_fields)])
        return select_query_records

    def _batch(self, records, n, char_limit=10000000):
        """Given an iterator of records, yields batches of
        records serialized in .csv format.

        Batches adhere to the following, in order of precedence:
        (1) They do not exceed the given character limit
        (2) They do not contain more than n records per batch
        """
        serialized_csv_fields = self._serialize_csv_record(self.fields)
        len_csv_fields = len(serialized_csv_fields)

        # append fields to first row
        batch = [serialized_csv_fields]
        current_chars = len_csv_fields
        for record in records:
            serialized_record = self._serialize_csv_record(record)
            # Does the next record put us over the character limit?
            if len(serialized_record) + current_chars > char_limit:
                yield batch
                batch = [serialized_csv_fields]
                current_chars = len_csv_fields

            batch.append(serialized_record)
            current_chars += len(serialized_record)

            # yield batch if we're at desired size
            # -1 due to first row being field names
            if len(batch) - 1 == n:
                yield batch
                batch = [serialized_csv_fields]
                current_chars = len_csv_fields

        # give back anything leftover
        if len(batch) > 1:
            yield batch

    def _serialize_csv_record(self, record):
        """Given a list of strings (record) return
        the corresponding record serialized in .csv format"""
        self.csv_writer.writerow(record)
        serialized = self.csv_buff.getvalue().encode("utf-8")
        # flush buffer
        self.csv_buff.truncate(0)
        self.csv_buff.seek(0)

        return serialized

    def get_results(self):
        """
        Retrieves and processes the results of a Bulk API operation.
        """

        if self.operation is DataOperationType.QUERY:
            yield from self._get_query_results()
        else:
            yield from self._get_batch_results()

    def _get_query_results(self):
        """Handles results for QUERY (select) operations"""
        for row in self.select_results:
            success = process_bool_arg(row["success"])
            created = process_bool_arg(row["created"])
            yield DataOperationResult(
                row["id"] if success else "",
                success,
                "",
                created,
            )

    def _get_batch_results(self):
        """Handles results for other DataOperationTypes (insert, update, etc.)"""
        for batch_id in self.batch_ids:
            try:
                results_url = (
                    f"{self.bulk.endpoint}/job/{self.job_id}/batch/{batch_id}/result"
                )
                # Download entire result file to a temporary file first
                # to avoid the server dropping connections
                with download_file(results_url, self.bulk) as f:
                    self.logger.info(f"Downloaded results for batch {batch_id}")
                    yield from self._parse_batch_results(f)

            except Exception as e:
                raise BulkDataException(
                    f"Failed to download results for batch {batch_id} ({str(e)})"
                )

    def _parse_batch_results(self, f):
        """Parses batch results from the downloaded file"""
        reader = csv.reader(f)
        next(reader)  # Skip header row

        for row in reader:
            success = process_bool_arg(row[1])
            created = process_bool_arg(row[2])
            yield DataOperationResult(
                row[0] if success else None,
                success,
                row[3] if not success else None,
                created,
            )


class RestApiDmlOperation(BaseDmlOperation):
    """Operation class for all DML operations run using the REST API."""

    def __init__(
        self,
        *,
        sobject,
        operation,
        api_options,
        context,
        fields,
        selection_strategy=SelectStrategy.SIMILARITY,
        selection_filter=None,
    ):
        super().__init__(
            sobject=sobject,
            operation=operation,
            api_options=api_options,
            context=context,
            fields=fields,
        )

        # Because we send values in JSON, we must convert Booleans and nulls
        describe = {
            field["name"]: field
            for field in getattr(context.sf, sobject).describe()["fields"]
        }
        self.boolean_fields = [f for f in fields if describe[f]["type"] == "boolean"]
        self.api_options = api_options.copy()
        self.api_options["batch_size"] = (
            self.api_options.get("batch_size") or DEFAULT_REST_BATCH_SIZE
        )
        self.api_options["batch_size"] = min(
            self.api_options["batch_size"], MAX_REST_BATCH_SIZE
        )

        self.select_operation_executor = SelectOperationExecutor(selection_strategy)
        self.selection_filter = selection_filter

    def _record_to_json(self, rec):
        result = dict(zip(self.fields, rec))
        for boolean_field in self.boolean_fields:
            try:
                result[boolean_field] = process_bool_arg(result[boolean_field] or False)
            except TypeError as e:
                raise BulkDataException(e)

        # Remove empty fields (different semantics in REST API)
        # We do this for insert only - on update, any fields set to `null`
        # are meant to be blanked out.
        if self.operation is DataOperationType.INSERT:
            result = {
                k: result[k]
                for k in result
                if result[k] is not None and result[k] != ""
            }
        elif self.operation in (DataOperationType.UPDATE, DataOperationType.UPSERT):
            result = {k: (result[k] if result[k] != "" else None) for k in result}

        result["attributes"] = {"type": self.sobject}
        return result

    def get_prev_record_values(self, records):
        """Get the previous values of the records based on the update key
        to ensure rollback can be performed"""
        # Function to be called only for UPSERT and UPDATE
        assert self.operation in [DataOperationType.UPSERT, DataOperationType.UPDATE]

        self.logger.info(f"Retrieving Previous Record Values of {self.sobject}")
        prev_record_values = []
        relevant_fields = set(self.fields + ["Id"])

        # Set update key
        update_key = (
            self.api_options.get("update_key")
            if self.operation == DataOperationType.UPSERT
            else "Id"
        )

        for chunk in iterate_in_chunks(self.api_options.get("batch_size"), records):
            update_key_values = tuple(
                filter(None, (self._record_to_json(rec)[update_key] for rec in chunk))
            )

            # Construct the query string
            query_fields = ", ".join(relevant_fields)
            query = f"SELECT {query_fields} FROM {self.sobject} WHERE {update_key} IN {update_key_values}"

            # Execute the query
            results = self.sf.query(query)

            # Extract relevant fields from results and extend the list
            prev_record_values.extend(
                [[res[key] for key in relevant_fields] for res in results["records"]]
            )

        self.logger.info("Done")
        return prev_record_values, tuple(relevant_fields)

    def load_records(self, records):
        """Load, update, upsert or delete records into the org"""

        self.results = []
        method = {
            DataOperationType.INSERT: "POST",
            DataOperationType.UPDATE: "PATCH",
            DataOperationType.DELETE: "DELETE",
            DataOperationType.UPSERT: "PATCH",
        }[self.operation]

        update_key = self.api_options.get("update_key")
        for chunk in iterate_in_chunks(self.api_options.get("batch_size"), records):
            if self.operation is DataOperationType.DELETE:
                url_string = "?ids=" + ",".join(
                    self._record_to_json(rec)["Id"] for rec in chunk
                )
                json = None
            else:
                if update_key:
                    assert self.operation == DataOperationType.UPSERT
                    url_string = f"/{self.sobject}/{update_key}"
                else:
                    url_string = ""
                json = {
                    "allOrNone": False,
                    "records": [self._record_to_json(rec) for rec in chunk],
                }

            self.results.extend(
                self.sf.restful(
                    f"composite/sobjects{url_string}", method=method, json=json
                )
            )

        row_errors = len([res for res in self.results if not res["success"]])
        self.job_result = DataOperationJobResult(
            DataOperationStatus.SUCCESS
            if not row_errors
            else DataOperationStatus.ROW_FAILURE,
            [],
            len(self.results),
            row_errors,
        )

    def select_records(self, records):
        """Executes a SOQL query to select records and adds them to results"""

        def convert(rec, fields):
            """Helper function to convert record values to strings, handling None values"""
            return [str(rec[f]) if rec[f] is not None else "" for f in fields]

        self.results = []
        query_records = []
        # Create a copy of the generator using tee
        records, records_copy = tee(records)
        # Count total number of records to fetch using the copy
        total_num_records = sum(1 for _ in records_copy)

        # Process in batches
        for offset in range(0, total_num_records, self.api_options.get("batch_size")):
            num_records = min(
                self.api_options.get("batch_size"), total_num_records - offset
            )

            # Generate the SOQL query based on the selection strategy
            (
                select_query,
                query_fields,
            ) = self.select_operation_executor.select_generate_query(
                sobject=self.sobject,
                fields=self.fields,
                limit=num_records,
                offset=offset,
            )

            # If user given selection filter present, create composite request
            if self.selection_filter:
                user_query = generate_user_filter_query(
                    filter_clause=self.selection_filter,
                    sobject=self.sobject,
                    fields=["Id"],
                    limit_clause=num_records,
                    offset_clause=offset,
                )
                query_records.extend(
                    self._execute_composite_query(
                        select_query=select_query,
                        user_query=user_query,
                        query_fields=query_fields,
                    )
                )
            else:
                # Handle the case where self.selection_query is None (and hence user_query is also None)
                response = self.sf.restful(
                    requests.utils.requote_uri(f"query/?q={select_query}"), method="GET"
                )
                query_records.extend(
                    list(convert(rec, query_fields) for rec in response["records"])
                )

        # Post-process the query results for this batch
        (
            selected_records,
            error_message,
        ) = self.select_operation_executor.select_post_process(
            load_records=records,
            query_records=query_records,
            num_records=total_num_records,
            sobject=self.sobject,
        )
        if not error_message:
            # Add selected records from this batch to the overall results
            self.results.extend(selected_records)

        # Update the job result based on the overall selection outcome
        self.job_result = DataOperationJobResult(
            status=DataOperationStatus.SUCCESS
            if len(self.results)  # Check the overall results length
            else DataOperationStatus.JOB_FAILURE,
            job_errors=[error_message] if error_message else [],
            records_processed=len(self.results),
            total_row_errors=0,
        )

    def _execute_composite_query(self, select_query, user_query, query_fields):
        """Executes a composite request with two queries and returns the intersected results."""

        def convert(rec, fields):
            """Helper function to convert record values to strings, handling None values"""
            return [str(rec[f]) if rec[f] is not None else "" for f in fields]

        composite_request_json = {
            "compositeRequest": [
                {
                    "method": "GET",
                    "url": requests.utils.requote_uri(
                        f"/services/data/v{self.sf.sf_version}/query/?q={select_query}"
                    ),
                    "referenceId": "select_query",
                },
                {
                    "method": "GET",
                    "url": requests.utils.requote_uri(
                        f"/services/data/v{self.sf.sf_version}/query/?q={user_query}"
                    ),
                    "referenceId": "user_query",
                },
            ]
        }
        response = self.sf.restful(
            "composite", method="POST", json=composite_request_json
        )

        # Extract results based on referenceId
        for sub_response in response["compositeResponse"]:
            if (
                sub_response["referenceId"] == "select_query"
                and sub_response["httpStatusCode"] == 200
            ):
                select_query_records = list(
                    convert(rec, query_fields)
                    for rec in sub_response["body"]["records"]
                )
            elif (
                sub_response["referenceId"] == "user_query"
                and sub_response["httpStatusCode"] == 200
            ):
                user_query_records = list(
                    convert(rec, ["Id"]) for rec in sub_response["body"]["records"]
                )
            else:
                raise SOQLQueryException(
                    f"{sub_response['body'][0]['errorCode']}: {sub_response['body'][0]['message']}"
                )
        # Find intersection based on 'Id'
        user_query_ids = list(record[0] for record in user_query_records)
        # Create a dictionary to map IDs to their corresponding records
        id_to_record_map = {
            record[query_fields.index("Id")]: record for record in select_query_records
        }

        # Extend query_records in the order of user_query_ids
        return [
            record
            for id in user_query_ids
            if (record := id_to_record_map.get(id)) is not None
        ]

    def get_results(self):
        """Return a generator of DataOperationResult objects."""

        def _convert(res):
            # TODO: make DataOperationResult handle this error variant
            if res.get("errors"):
                errors = "\n".join(
                    f"{e['statusCode']}: {e['message']} ({','.join(e['fields'])})"
                    for e in res["errors"]
                )
            else:
                errors = ""

            if self.operation == DataOperationType.INSERT:
                created = True
            elif self.operation == DataOperationType.UPDATE:
                created = False
            else:
                created = res.get("created")

            return DataOperationResult(res.get("id"), res["success"], errors, created)

        yield from (_convert(res) for res in self.results)


def get_query_operation(
    *,
    sobject: str,
    fields: List[str],
    api_options: Dict,
    context: Any,
    query: str,
    api: Optional[DataApi] = DataApi.SMART,
) -> BaseQueryOperation:
    """Create an appropriate QueryOperation instance for the given parameters, selecting
    between REST and Bulk APIs based upon volume (Bulk > 2000 records) if DataApi.SMART
    is provided."""

    # The Record Count endpoint requires API 40.0. REST Collections requires 42.0.
    api_version = float(context.sf.sf_version)
    if api_version < 42.0 and api is not DataApi.BULK:
        api = DataApi.BULK

    if api in (DataApi.SMART, None):
        record_count_response = context.sf.restful(
            f"limits/recordCount?sObjects={sobject}"
        )
        sobject_map = {
            entry["name"]: entry["count"] for entry in record_count_response["sObjects"]
        }
        api = (
            DataApi.BULK
            if sobject in sobject_map and sobject_map[sobject] >= 2000
            else DataApi.REST
        )

    if api is DataApi.BULK:
        return BulkApiQueryOperation(
            sobject=sobject, api_options=api_options, context=context, query=query
        )
    elif api is DataApi.REST:
        return RestApiQueryOperation(
            sobject=sobject,
            api_options=api_options,
            context=context,
            query=query,
            fields=fields,
        )
    else:
        raise AssertionError(f"Unknown API: {api}")


def get_dml_operation(
    *,
    sobject: str,
    operation: DataOperationType,
    fields: List[str],
    api_options: Dict,
    context: Any,
    volume: int,
    api: Optional[DataApi] = DataApi.SMART,
    selection_strategy: SelectStrategy = SelectStrategy.STANDARD,
    selection_filter: Union[str, None] = None,
) -> BaseDmlOperation:
    """Create an appropriate DmlOperation instance for the given parameters, selecting
    between REST and Bulk APIs based upon volume (Bulk used at volumes over 2000 records,
    or if the operation is HARD_DELETE, which is only available for Bulk)."""

    context.logger.debug(f"Creating {operation} Operation for {sobject} using {api}")
    assert isinstance(operation, DataOperationType)

    # REST Collections requires 42.0.
    api_version = float(context.sf.sf_version)
    if api_version < 42.0 and api is not DataApi.BULK:
        api = DataApi.BULK

    if api in (DataApi.SMART, None):
        api = (
            DataApi.BULK
            if volume >= 2000 or operation is DataOperationType.HARD_DELETE
            else DataApi.REST
        )

    if api is DataApi.BULK:
        api_class = BulkApiDmlOperation
    elif api is DataApi.REST:
        api_class = RestApiDmlOperation
    else:
        raise AssertionError(f"Unknown API: {api}")

    return api_class(
        sobject=sobject,
        operation=operation,
        api_options=api_options,
        context=context,
        fields=fields,
        selection_strategy=selection_strategy,
        selection_filter=selection_filter,
    )


def generate_user_filter_query(
    filter_clause: str,
    sobject: str,
    fields: list,
    limit_clause: Union[int, None] = None,
    offset_clause: Union[int, None] = None,
) -> str:
    """
    Generates a SOQL query with the provided filter, object, fields, limit, and offset clauses.
    Handles cases where the filter clause already contains LIMIT or OFFSET, and avoids multiple spaces.
    """

    # Extract existing LIMIT and OFFSET from filter_clause if present
    existing_limit_match = re.search(r"LIMIT\s+(\d+)", filter_clause, re.IGNORECASE)
    existing_offset_match = re.search(r"OFFSET\s+(\d+)", filter_clause, re.IGNORECASE)

    if existing_limit_match:
        existing_limit = int(existing_limit_match.group(1))
        if limit_clause is not None:  # Only apply limit_clause if it's provided
            limit_clause = min(existing_limit, limit_clause)
        else:
            limit_clause = existing_limit

    if existing_offset_match:
        existing_offset = int(existing_offset_match.group(1))
        if offset_clause is not None:
            offset_clause = existing_offset + offset_clause
        else:
            offset_clause = existing_offset

    # Remove existing LIMIT and OFFSET from filter_clause, handling potential extra spaces
    filter_clause = re.sub(
        r"\s+OFFSET\s+\d+\s*", " ", filter_clause, flags=re.IGNORECASE
    ).strip()
    filter_clause = re.sub(
        r"\s+LIMIT\s+\d+\s*", " ", filter_clause, flags=re.IGNORECASE
    ).strip()

    # Construct the SOQL query
    fields_str = ", ".join(fields)
    query = f"SELECT {fields_str} FROM {sobject} {filter_clause}"

    if limit_clause is not None:
        query += f" LIMIT {limit_clause}"
    if offset_clause is not None:
        query += f" OFFSET {offset_clause}"

    return query
