"""
Loads a mysql database into the warehouse through the pipeline via Sqoop.
"""
import datetime
import json
import logging
import re

import luigi

from edx.analytics.tasks.common.bigquery_load import BigQueryLoadDownstreamMixin, BigQueryLoadTask, BigQueryTarget
from edx.analytics.tasks.common.mysql_load import get_mysql_query_results
from edx.analytics.tasks.common.snowflake_load import (
    SnowflakeLoadDownstreamMixin, SnowflakeLoadFromHiveTSVTask, SnowflakeTarget
)
from edx.analytics.tasks.common.sqoop import METADATA_FILENAME, SqoopImportFromMysql
from edx.analytics.tasks.common.vertica_load import SchemaManagementTask, VerticaCopyTask
from edx.analytics.tasks.util.hive import HivePartition, WarehouseMixin
from edx.analytics.tasks.util.overwrite import OverwriteOutputMixin
from edx.analytics.tasks.util.url import ExternalURL, get_target_from_url, url_path_join

try:
    from google.cloud.bigquery import SchemaField
    bigquery_available = True  # pylint: disable=invalid-name
except ImportError:
    bigquery_available = False  # pylint: disable=invalid-name


log = logging.getLogger(__name__)

# define pseudo-table-name to use for storing database-level metadata output.
DUMP_METADATA_OUTPUT = '_metadata_export_database'


class MysqlToWarehouseTaskMixin(WarehouseMixin):
    """
    Parameters for importing a mysql database into the warehouse.
    """

    exclude_field = luigi.ListParameter(
        default=(),
        description='List of regular expression patterns for matching "tablename.fieldname" fields that should not be output.',
    )
    date = luigi.DateParameter(
        default=datetime.datetime.utcnow().date(),
    )
    db_credentials = luigi.Parameter(
        config_path={'section': 'database-import', 'name': 'credentials'},
        description='Path to the external access credentials file.',
    )
    database = luigi.Parameter(
        config_path={'section': 'database-import', 'name': 'database'}
    )

    def should_exclude_field(self, table_name, field_name):
        """Determines whether to exclude an individual field during the import, matching against 'table.field'."""
        full_name = "{}.{}".format(table_name, field_name)
        if any(re.match(pattern, full_name) for pattern in self.exclude_field):
            return True
        return False

    @property
    def field_delimiter(self):
        """The delimiter in the data to be copied."""
        # Select a delimiter string that will not occur in field values.  Use the same for Vertica and BigQuery.
        return '\x01'

    @property
    def null_marker(self):
        """The null sequence in the data to be copied."""
        # Using "NULL" doesn't work, because there are values in several tables
        # in non-nullable fields that store the string value "NULL" (or "Null" or "null").
        # As with the field delimiter, we must use something here that we don't expect to
        # appear in a field value.  (This was an arbitrary but hopefully still readable choice.)
        # Note that this option only works if "direct" mode is disabled, otherwise it will be ignored
        # and "NULL" will always be output.  Use the same for Vertica and BigQuery.
        return 'NNULLL'

    @property
    def quote_character(self):
        """The character used to enclose field values to treat as literal when they contain special characters.  Default is none."""
        # BigQuery does not handle escaping of quotes.  It hews to a narrower standard for CSV
        # input, which expects quote characters to be doubled as a way of escaping them.
        # We therefore have to avoid escaping quotes by selecting delimiters and null markers
        # so they won't appear in field values at all.  We therefore do without any quote character at all.
        # Use the same for Vertica and BigQuery.
        return ''


class MysqlToS3TaskMixin(MysqlToWarehouseTaskMixin):
    """
    Parameters for exporting a mysql database to S3.
    """

    # Don't use the same source for BigQuery loads as was used for Vertica loads,
    # until their formats and exclude-field parameters match.
    warehouse_subdirectory = luigi.Parameter(
        default='import_mysql_to_s3',
        description='Subdirectory under warehouse_path to store intermediate data.'
    )


class MysqlTableExportMixin(MysqlToS3TaskMixin):
    """Methods used by classes that export or load MySQL tables."""

    def mysql_table_schema(self):
        return self.metadata['additional_metadata']['table_schema']

    def get_table_metadata_target(self):
        """Returns target for metadata file from the given dump."""
        # find the .metadata file in the source directory.
        metadata_path = url_path_join(self.s3_location_for_table, METADATA_FILENAME)
        return get_target_from_url(metadata_path)

    @property
    def s3_location_for_table(self):
        partition_path_spec = HivePartition('dt', self.date.isoformat()).path_spec
        destination = url_path_join(
            self.warehouse_path,
            self.warehouse_subdirectory,
            self.database,
            self.table_name,
            partition_path_spec
        ) + '/'
        return destination

    def get_table_metadata(self):
        metadata_target = self.get_table_metadata_target()
        with metadata_target.open('r') as metadata_file:
            metadata = json.load(metadata_file)
        return metadata


class ExportMysqlTableToS3Task(MysqlTableExportMixin, OverwriteOutputMixin, luigi.Task):
    """
    Task to export a table from MySQL to S3.
    """

    table_name = luigi.Parameter(
        description='The name of the table.',
    )

    def __init__(self, *args, **kwargs):
        super(ExportMysqlTableToS3Task, self).__init__(*args, **kwargs)
        self.required_tasks = None
        self.sqoop_export_task = None
        self.deleted_fields = []

    def requires(self):
        if self.required_tasks is None:
            self.required_tasks = {
                'sqoop_export_task': self.sqoop_export_mysql_table_task
            }
        return self.required_tasks

    def output(self):
        return get_target_from_url(self.s3_location_for_table)

    def complete(self):
        return self.sqoop_export_mysql_table_task.complete()

    def mysql_compliant_schema(self):
        table_schema = []
        results = get_mysql_query_results(self.db_credentials, self.database, 'describe {}'.format(self.table_name))
        for result in results:
            field_name = result[0].strip()
            field_type = result[1].strip()
            field_null = result[2].strip()
            if self.should_exclude_field(self.table_name, field_name):
                self.deleted_fields.append(field_name)
            else:
                table_schema.append((field_name, field_type, field_null))
        return table_schema

    @property
    def sqoop_export_mysql_table_task(self):
        if self.sqoop_export_task is None:
            mysql_schema = self.mysql_compliant_schema()
            column_names = [field_name for (field_name, _field_type, _field_null) in mysql_schema]
            additional_metadata = {
                'table_schema': mysql_schema,
                'deleted_fields': self.deleted_fields,
                'database': self.database,
                'table_name': self.table_name,
                'date': self.date.isoformat(),
            }
            self.sqoop_export_task = SqoopImportFromMysql(
                table_name=self.table_name,
                credentials=self.db_credentials,
                database=self.database,
                destination=self.s3_location_for_table,
                overwrite=self.overwrite,
                mysql_delimiters=False,
                fields_terminated_by=self.field_delimiter,
                null_string=self.null_marker,
                delimiter_replacement=' ',
                direct=False,
                columns=column_names,
                additional_metadata=additional_metadata,
            )

        return self.sqoop_export_task


class MysqlDatabaseExportMixin(MysqlToS3TaskMixin):
    """Parameters and methods used by classes that export or load MySQL databases."""

    include = luigi.ListParameter(
        default=(),
        description='List of regular expression patterns for matching the names of tables that should be output.',
    )

    exclude = luigi.ListParameter(
        default=(),
        description='List of regular expression patterns for matching the names of tables that should not be output.',
    )

    def should_exclude_table(self, table_name):
        """Determines whether to exclude a table during the import."""

        if self.include:
            return not any(re.match(pattern, table_name) for pattern in self.include)
        elif self.exclude:
            return any(re.match(pattern, table_name) for pattern in self.exclude)

        return False

    def get_table_list_for_database(self):
        return self.metadata['table_list']

    def database_metadata_target(self):
        partition_path_spec = HivePartition('dt', self.date.isoformat()).path_spec
        metadata_destination = url_path_join(
            self.warehouse_path,
            self.warehouse_subdirectory,
            self.database,
            DUMP_METADATA_OUTPUT,
            partition_path_spec,
            METADATA_FILENAME,
        )
        return get_target_from_url(metadata_destination)


class ExportMysqlDatabaseToS3Task(MysqlDatabaseExportMixin, luigi.Task):
    """
    Provides entry point for exporting a MySQL database to S3.
    """

    overwrite = luigi.BoolParameter(
        default=False,
        significant=False,
    )

    def __init__(self, *args, **kwargs):
        super(ExportMysqlDatabaseToS3Task, self).__init__(*args, **kwargs)
        self.table_includes_list = []
        self.creation_time = None
        self.required_tasks = None

    def requires(self):
        if self.creation_time is None:
            self.creation_time = datetime.datetime.utcnow().isoformat()

        if self.required_tasks is None:
            self.required_tasks = []
            if not self.table_includes_list:
                results = get_mysql_query_results(self.db_credentials, self.database, 'show tables')
                table_list = [result[0].strip() for result in results]
                self.table_includes_list = [table_name for table_name in table_list if not self.should_exclude_table(table_name)]

            for table_name in self.table_includes_list:
                self.required_tasks.append(
                    ExportMysqlTableToS3Task(
                        date=self.date,
                        database=self.database,
                        db_credentials=self.db_credentials,
                        exclude_field=self.exclude_field,
                        table_name=table_name,
                    )
                )
        return self.required_tasks

    def output(self):
        return self.database_metadata_target()

    def run(self):
        metadata = {
            'table_list': self.table_includes_list,
            'database': self.database,
            'date': self.date.isoformat(),
            'exclude': self.exclude,
            'include': self.include,
            'warehouse_path': self.warehouse_path,
            'warehouse_subdirectory': self.warehouse_subdirectory,
            'creation_time': self.creation_time,
            'completion_time': datetime.datetime.utcnow().isoformat(),
        }
        with self.output().open('w') as metadata_file:
            json.dump(metadata, metadata_file)


class LoadMysqlTableFromS3ToVerticaTask(MysqlTableExportMixin, VerticaCopyTask):
    """
    Task to import a MySQL table from S3 into vertica.
    """
    table_name = luigi.Parameter(
        description='The name of the table.',
    )
    # Some fields are no longer needed when reading from S3.
    exclude_field = None
    db_credentials = None

    def __init__(self, *args, **kwargs):
        super(LoadMysqlTableFromS3ToVerticaTask, self).__init__(*args, **kwargs)
        self.metadata = self.get_table_metadata()

    def requires(self):
        if self.required_tasks is None:
            self.required_tasks = {
                'credentials': ExternalURL(url=self.credentials),
                'insert_source': self.insert_source_task,
            }
        return self.required_tasks

    def vertica_compliant_schema(self):
        """Transforms mysql table schema into a vertica compliant schema."""

        vertica_schema = []
        for field_name, field_type, field_null in self.mysql_table_schema():

            types_with_parentheses = ['tinyint', 'smallint', 'int', 'bigint', 'datetime']
            if field_type == 'tinyint(1)':
                field_type = 'BOOLEAN'
            elif any(_type in field_type for _type in types_with_parentheses):
                field_type = field_type.rsplit('(')[0]
            elif field_type == 'longtext':
                field_type = 'LONG VARCHAR'
            elif field_type == 'longblob':
                field_type = 'LONG VARBINARY'
            elif field_type == 'double':
                field_type = 'DOUBLE PRECISION'

            if field_null == "NO":
                field_type = field_type + " NOT NULL"

            field_name = "\"{}\"".format(field_name)
            vertica_schema.append((field_name, field_type))

        return vertica_schema

    @property
    def copy_delimiter(self):
        """The delimiter in the data to be copied."""
        return "'{}'".format(self.field_delimiter)

    @property
    def copy_null_sequence(self):
        """The null sequence in the data to be copied."""
        return "'{}'".format(self.null_marker)

    @property
    def copy_enclosed_by(self):
        """The field's enclosing character. Default is empty string."""
        return "'{}'".format(self.quote_character)

    @property
    def copy_escape_spec(self):
        """
        The escape character to use to have special characters be treated literally.

        Copy's default is backslash if this is a zero-length string.   To disable escaping,
        use "NO ESCAPE".  To use a different character, use "ESCAPE AS 'char'".
        """
        # We disable escaping here, because we trust the (hopefully obscure) delimiters.
        return "NO ESCAPE"

    @property
    def insert_source_task(self):
        return ExternalURL(url=self.s3_location_for_table)

    @property
    def default_columns(self):
        return []

    @property
    def table(self):
        return self.table_name

    @property
    def columns(self):
        return self.vertica_compliant_schema()

    @property
    def auto_primary_key(self):
        return None


class PreImportDatabaseTask(SchemaManagementTask):
    """
    Task needed to run before importing database into warehouse.
    """

    @property
    def queries(self):
        return [
            "DROP SCHEMA IF EXISTS {schema} CASCADE;".format(schema=self.schema_loading),
            "CREATE SCHEMA IF NOT EXISTS {schema}".format(schema=self.schema_loading),
        ]

    @property
    def marker_name(self):
        return 'pre_database_import_{schema}_{date}'.format(
            schema=self.schema,
            date=self.date.strftime('%Y-%m-%d')
        )


class PostImportDatabaseTask(SchemaManagementTask):
    """
    Task needed to run after importing database into warehouse.
    """

    # Override the standard roles here since these tables will be rather raw. We may want to restrict access to a
    # subset of users.
    roles = luigi.ListParameter(
        config_path={'section': 'vertica-export', 'name': 'business_intelligence_team_roles'},
    )
    tables = luigi.ListParameter(
        default=[],
        description="List of tables in schema.",
        significant=False
    )

    def analyze_stats_queries(self):
        """Provides queries to run analyze statistics for each table in schema."""
        queries = []
        for table in self.tables:
            queries.append(
                "SELECT ANALYZE_STATISTICS('{schema}.{table}');".format(
                    schema=self.schema,
                    table=table
                )
            )
        return queries

    @property
    def queries(self):
        queries = [
            "DROP SCHEMA IF EXISTS {schema} CASCADE;".format(schema=self.schema),
            "ALTER SCHEMA {schema_loading} RENAME TO {schema};".format(
                schema_loading=self.schema_loading,
                schema=self.schema
            ),
            "GRANT USAGE ON SCHEMA {schema} TO {roles};".format(schema=self.schema, roles=self.vertica_roles),
            "GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {roles};".format(
                schema=self.schema,
                roles=self.vertica_roles
            ),
        ]
        queries += self.analyze_stats_queries()
        return queries

    @property
    def marker_name(self):
        return 'post_database_import_{schema}_{date}'.format(
            schema=self.schema,
            date=self.date.strftime('%Y-%m-%d')
        )


class ImportMysqlDatabaseFromS3ToVerticaSchemaTask(MysqlDatabaseExportMixin, luigi.WrapperTask):
    """
    Provides entry point for importing a mysql database into Vertica via S3.
    This workflow assumes that MySQL database has already been exported to S3.
    """

    schema = luigi.Parameter(
        config_path={'section': 'vertica-export', 'name': 'schema'},
        description='The schema to which to write.',
    )
    credentials = luigi.Parameter(
        config_path={'section': 'vertica-export', 'name': 'credentials'},
        description='Path to the external access credentials file.',
    )
    overwrite = luigi.BoolParameter(
        default=False,
        significant=False,
    )

    marker_schema = luigi.Parameter(
        description='The marker schema to which to write the marker table.'
    )

    # Some fields are no longer needed when reading from S3.
    exclude_field = None
    db_credentials = None

    def __init__(self, *args, **kwargs):
        super(ImportMysqlDatabaseFromS3ToVerticaSchemaTask, self).__init__(*args, **kwargs)
        metadata_target = self.database_metadata_target()
        with metadata_target.open('r') as metadata_file:
            self.metadata = json.load(metadata_file)
        self.is_complete = False

    def run(self):
        # Add yields of tasks in run() method, to serve as dynamic dependencies.
        # This method should be rerun each time it yields a job.

        pre_import_task = PreImportDatabaseTask(
            date=self.date,
            schema=self.schema,
            credentials=self.credentials,
            marker_schema=self.marker_schema,
            overwrite=self.overwrite,
        )
        yield pre_import_task

        table_white_list = []
        for table_name in self.get_table_list_for_database():
            if not self.should_exclude_table(table_name):
                table_white_list.append(table_name)
                yield LoadMysqlTableFromS3ToVerticaTask(
                    credentials=self.credentials,
                    schema=pre_import_task.schema_loading,
                    database=self.database,
                    warehouse_path=self.warehouse_path,
                    warehouse_subdirectory=self.warehouse_subdirectory,
                    table_name=table_name,
                    overwrite=self.overwrite,
                    date=self.date,
                    marker_schema=self.marker_schema,
                )

        yield PostImportDatabaseTask(
            date=self.date,
            schema=self.schema,
            credentials=self.credentials,
            marker_schema=self.marker_schema,
            overwrite=self.overwrite,
            tables=table_white_list
        )

        self.is_complete = True

    def complete(self):
        return self.is_complete


# Accepted types for standard tables are 'STRING', 'INT64', 'FLOAT64', 'BOOL', 'TIMESTAMP', 'BYTES', 'DATE', 'TIME', 'DATETIME'.
MYSQL_TO_BIGQUERY_TYPE_MAP = {
    # STRING -- BQ assumes input is utf8.
    # Loading will fail if the string data being loaded is not utf8, or contains ASCII 0.
    'char': 'STRING',  # includes 'NCHAR'.
    'varchar': 'STRING',  # includes 'NVARCHAR'.
    'text': 'STRING',
    'tinytext': 'STRING',
    'mediumtext': 'STRING',
    'longtext': 'STRING',
    'enum': 'STRING',
    # BIT is actually output by Sqoop as a Hex number.  Requires conversion on BigQuery side from Hex string to number,
    # by prepending '0x' to the string and safe-casting to INT64.
    'bit': 'STRING',
    # BOOL
    'tinyint(1)': 'BOOL',  # includes "BOOL" and "BOOLEAN".
    # INT64
    'tinyint': 'INT64',
    'smallint': 'INT64',
    'mediumint': 'INT64',
    'int': 'INT64',  # includes 'INTEGER'.
    'bigint': 'INT64',
    'year': 'INT64',
    # FLOAT64
    'decimal': 'FLOAT64',  # includes 'DEC', 'DECIMAL', 'NUMERIC', 'FIXED'.
    'float': 'FLOAT64',
    'double': 'FLOAT64',  # includes 'DOUBLE', 'DOUBLE PRECISION', 'REAL'.
    # BYTES
    # Sqoop outputs these as two character hex-codes separated by a space.  BigQuery reads these in and displays them as
    # the corresponding hex values without the space separation.  This doesn't seem like the desired behavior, but the
    # problem probably lies with Sqoop.  At present, LMS has only two columns that are longblobs, and they are passwords
    # in a table that is excluded.
    'binary': 'BYTES',
    'varbinary': 'BYTES',
    'tinyblob': 'BYTES',
    'blob': 'BYTES',
    'mediumblob': 'BYTES',
    'longblob': 'BYTES',
    # DATE
    'date': 'DATE',
    # TIME
    'time': 'TIME',
    # DATETIME
    'datetime': 'DATETIME',
    # TIMESTAMP -- like DATETIME, but with timezone
    'timestamp': 'TIMESTAMP',
}


class MysqlToBigQueryTaskMixin(MysqlToWarehouseTaskMixin):
    """
    Parameters for importing a mysql database into BigQuery.
    """

    # Don't use the same source for BigQuery loads as was used for Vertica loads,
    # until their formats and exclude-field parameters match.
    warehouse_subdirectory = luigi.Parameter(
        default='import_mysql_to_bq',
        description='Subdirectory under warehouse_path to store intermediate data.'
    )


class LoadMysqlToBigQueryTableTask(MysqlToBigQueryTaskMixin, BigQueryLoadTask):
    """
    Task to import a table from MySQL into BigQuery.
    """

    table_name = luigi.Parameter(
        description='The name of the table.',
    )

    def __init__(self, *args, **kwargs):
        super(LoadMysqlToBigQueryTableTask, self).__init__(*args, **kwargs)
        # Get schema for BigQuery and also Mysql columns that are excluded from that schema.
        self.mysql_table_schema = []
        self.table_schema = []
        self.deleted_fields = []

    def mysql_compliant_schema(self):
        if not self.mysql_table_schema:
            results = get_mysql_query_results(self.db_credentials, self.database, 'describe {}'.format(self.table_name))
            for result in results:
                field_name = result[0].strip()
                field_type = result[1].strip()
                field_null = result[2].strip()
                if self.should_exclude_field(self.table_name, field_name):
                    self.deleted_fields.append(field_name)
                else:
                    self.mysql_table_schema.append((field_name, field_type, field_null))
        return self.mysql_table_schema

    def get_bigquery_schema(self):
        """Transforms mysql table schema into a vertica compliant schema."""

        if not self.table_schema:
            mysql_columns = self.mysql_compliant_schema()
            for (field_name, field_type, field_null) in mysql_columns:

                # Strip off size information from any type except booleans.
                if field_type != 'tinyint(1)':
                    field_type = field_type.rsplit('(')[0]

                bigquery_type = MYSQL_TO_BIGQUERY_TYPE_MAP.get(field_type)
                mode = 'REQUIRED' if field_null == 'NO' else 'NULLABLE'
                description = ''

                self.table_schema.append(SchemaField(field_name, bigquery_type, description=description, mode=mode))

        return self.table_schema

    @property
    def insert_source_task(self):
        # Make sure yet again that columns have been calculated.
        columns = [field.name for field in self.schema]
        partition_path_spec = HivePartition('dt', self.date.isoformat()).path_spec
        destination = url_path_join(
            self.warehouse_path,
            self.warehouse_subdirectory,
            self.database,
            self.table_name,
            partition_path_spec
        ) + '/'

        additional_metadata = {
            'table_schema': self.mysql_compliant_schema(),
            'deleted_fields': self.deleted_fields,
            'database': self.database,
            'table_name': self.table_name,
            'date': self.date.isoformat(),
        }

        return SqoopImportFromMysql(
            table_name=self.table_name,
            credentials=self.db_credentials,
            database=self.database,
            destination=destination,
            overwrite=self.overwrite,
            mysql_delimiters=False,
            fields_terminated_by=self.field_delimiter,
            null_string=self.null_marker,
            delimiter_replacement=' ',
            direct=False,
            columns=columns,
            additional_metadata=additional_metadata,
        )

    @property
    def table(self):
        return self.table_name

    @property
    def schema(self):
        return self.get_bigquery_schema()

    @property
    def table_description(self):
        optional = ''
        if self.deleted_fields:
            optional = '  Fields not included in the copy: {}'.format(', '.join(self.deleted_fields))
        return "Copy of '{}' table from '{}' MySQL database on {}.{}".format(self.table_name, self.database, self.date.isoformat(), optional)

    @property
    def table_friendly_name(self):
        return '{} from {}'.format(self.table_name, self.database)


class ImportMysqlDatabaseToBigQueryDatasetTask(MysqlToBigQueryTaskMixin, BigQueryLoadDownstreamMixin, luigi.WrapperTask):
    """
    Provides entry point for importing a mysql database into BigQuery as a single dataset.

    It is assumed that there is one database written to each dataset.
    """

    exclude = luigi.ListParameter(
        default=(),
        description='List of regular expressions matching database table names that should not be imported from MySQL to BigQuery.'
    )

    def __init__(self, *args, **kwargs):
        super(ImportMysqlDatabaseToBigQueryDatasetTask, self).__init__(*args, **kwargs)
        self.table_list = []
        self.is_complete = False
        self.required_tasks = None

        # If we are overwriting the database output, then delete the entire marker table.
        # That way, any future activity on it should only consist of inserts, rather than any deletes
        # of existing marker entries.  There are quotas on deletes and upserts on a table, of no more
        # than 96 per day.   This allows us to work around hitting those limits.
        # Note that we have to do this early, before scheduling begins, so that no entries are present
        # when scheduling occurs (so everything gets properly scheduled).
        if self.overwrite:
            # First, create a BigQueryTarget object, so we can connect to BigQuery.  This is only
            # for the purpose of deleting the marker table, so use dummy values.
            credentials_target = ExternalURL(url=self.credentials).output()
            target = BigQueryTarget(
                credentials_target=credentials_target,
                dataset_id=self.dataset_id,
                table="dummy_table",
                update_id="dummy_id",
            )
            # Now ask it to delete the marker table completely.
            target.delete_marker_table()

    def should_exclude_table(self, table_name):
        """Determines whether to exclude a table during the import."""
        if any(re.match(pattern, table_name) for pattern in self.exclude):
            return True
        return False

    def requires(self):
        if not self.table_list:
            results = get_mysql_query_results(self.db_credentials, self.database, 'show tables')
            unfiltered_table_list = [result[0].strip() for result in results]
            self.table_list = [table_name for table_name in unfiltered_table_list if not self.should_exclude_table(table_name)]
        if self.required_tasks is None:
            self.required_tasks = []
            for table_name in self.table_list:
                self.required_tasks.append(
                    LoadMysqlToBigQueryTableTask(
                        db_credentials=self.db_credentials,
                        database=self.database,
                        warehouse_path=self.warehouse_path,
                        warehouse_subdirectory=self.warehouse_subdirectory,
                        table_name=table_name,
                        overwrite=self.overwrite,
                        date=self.date,
                        dataset_id=self.dataset_id,
                        credentials=self.credentials,
                        max_bad_records=self.max_bad_records,
                        skip_clear_marker=self.overwrite,
                        exclude_field=self.exclude_field,
                    )
                )
        return self.required_tasks

    def output(self):
        return [task.output() for task in self.requires()]

    def complete(self):
        # OverwriteOutputMixin changes the complete() method behavior, so we override it.
        return all(r.complete() for r in luigi.task.flatten(self.requires()))


# Snowflake reserves these keywords - they cannot be used as column (field) names in a table unless double-quoted.
SNOWFLAKE_RESERVED_KEYWORDS = (
    'ALL', 'ALTER', 'AND', 'ANY', 'AS', 'ASC', 'BETWEEN', 'BY', 'CASE', 'CAST', 'CHECK', 'CLUSTER',
    'COLUMN', 'CONNECT', 'CREATE', 'CROSS', 'CURRENT_DATE', 'CURRENT_ROLE', 'CURRENT_USER', 'CURRENT_TIME', 'CURRENT_TIMESTAMP',
    'DELETE', 'DESC', 'DISTINCT', 'DROP', 'ELSE', 'EXCLUSIVE', 'EXISTS', 'FALSE', 'FOR', 'FROM', 'FULL',
    'GRANT', 'GROUP', 'HAVING', 'IDENTIFIED', 'ILIKE', 'IMMEDIATE', 'IN', 'INCREMENT', 'INNER', 'INSERT',
    'INTERSECT', 'INTO', 'IS', 'JOIN', 'LATERAL', 'LEFT', 'LIKE', 'LOCK', 'LONG', 'MAXEXTENTS', 'MINUS',
    'MODIFY', 'NATURAL', 'NOT', 'NULL', 'OF', 'ON', 'OPTION', 'OR', 'ORDER', 'REGEXP', 'RENAME', 'REVOKE',
    'RIGHT', 'RLIKE', 'ROW', 'ROWS', 'SAMPLE', 'SELECT', 'SET', 'SOME', 'START', 'TABLE', 'TABLESAMPLE',
    'THEN', 'TO', 'TRIGGER', 'TRUE', 'UNION', 'UNIQUE', 'UPDATE', 'USING', 'VALUES', 'VIEW', 'WHEN',
    'WHENEVER', 'WHERE', 'WITH'
)


class LoadMysqlTableFromS3ToSnowflakeTask(MysqlTableExportMixin, SnowflakeLoadFromHiveTSVTask):
    """
    Task to import a table from S3 but originally from MySQL into Snowflake.
    """
    table_name = luigi.Parameter(
        description='The name of the table to be loaded.',
    )
    # Some fields are no longer needed when reading from S3.
    exclude_field = None
    db_credentials = None

    def __init__(self, *args, **kwargs):
        """
        Init this task.
        """
        super(LoadMysqlTableFromS3ToSnowflakeTask, self).__init__(*args, **kwargs)
        self.metadata = self.get_table_metadata()

    def get_snowflake_schema(self):
        """
        Transforms MySQL table schema into a Snowflake-compliant schema.
        """
        results = []
        for field_name, field_type, field_null in self.mysql_table_schema():

            # Enclose any Snowflake-reserved keyword field names within double-quotes.
            if field_name.upper() in SNOWFLAKE_RESERVED_KEYWORDS:
                field_name = '"{}"'.format(field_name.upper())

            mysql_types_with_parentheses = ['smallint', 'int', 'bigint', 'datetime', 'varchar']
            if field_type == 'tinyint(1)':
                field_type = 'BOOLEAN'
            elif any(_type in field_type for _type in mysql_types_with_parentheses):
                field_type = field_type.rsplit('(')[0]
            elif field_type == 'longtext':
                field_type = 'VARCHAR'
            elif field_type == 'longblob':
                field_type = 'BINARY'

            if field_null == 'NO':
                field_type += ' NOT NULL'

            results.append((field_name, field_type))

        return results

    @property
    def insert_source_task(self):
        """
        Insert the Sqoop task that imports the source MySQL data into S3.
        """
        return ExternalURL(url=self.s3_location_for_table)

    @property
    def table(self):
        return self.table_name

    @property
    def table_schema(self):
        return self.get_snowflake_schema()

    @property
    def columns(self):
        return self.table_schema

    @property
    def file_format_name(self):
        return 'SQOOP_MYSQL_FORMAT'

    @property
    def pattern(self):
        return '.*part-m.*'

    @property
    def table_description(self):
        optional = ''
        deleted_fields = self.metadata['additional_metadata']['deleted_fields']
        if deleted_fields:
            optional = '  Fields not included in the copy: {}'.format(', '.join(deleted_fields))
        return "Copy of '{}' table from '{}' MySQL database on {}.{}".format(self.table_name, self.database, self.date.isoformat(), optional)


class ImportMysqlDatabaseFromS3ToSnowflakeSchemaTask(MysqlDatabaseExportMixin, SnowflakeLoadDownstreamMixin, luigi.WrapperTask):
    """
    Provides entry point for importing a MySQL database into Snowflake as a single schema.

    Assumes that only one database is written to each schema.
    """
    # Some fields are no longer needed when reading from S3.
    exclude_field = None
    db_credentials = None

    def __init__(self, *args, **kwargs):
        """
        Inits this Luigi task.
        """
        super(ImportMysqlDatabaseFromS3ToSnowflakeSchemaTask, self).__init__(*args, **kwargs)
        metadata_target = self.database_metadata_target()
        with metadata_target.open('r') as metadata_file:
            self.metadata = json.load(metadata_file)

        self.required_tasks = None

    def requires(self):
        """
        Determines the required tasks given the list of tables exported from MySQL.
        """
        if self.required_tasks is None:
            self.required_tasks = []
            for table_name in self.get_table_list_for_database():
                if not self.should_exclude_table(table_name):
                    self.required_tasks.append(
                        LoadMysqlTableFromS3ToSnowflakeTask(
                            sf_database=self.sf_database,
                            schema=self.schema,
                            scratch_schema=self.scratch_schema,
                            run_id=self.run_id,
                            warehouse=self.warehouse,
                            role=self.role,
                            warehouse_path=self.warehouse_path,
                            warehouse_subdirectory=self.warehouse_subdirectory,
                            database=self.database,
                            table_name=table_name,
                            overwrite=self.overwrite,
                            date=self.date,
                            credentials=self.credentials,
                        )
                    )
        return self.required_tasks

    def complete(self):
        """
        Reduces the result of the required tasks into a single complete/not-complete.
        """
        # OverwriteOutputMixin changes the complete() method behavior, so we override it.
        return all(r.complete() for r in luigi.task.flatten(self.requires()))
