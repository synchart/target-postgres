from copy import deepcopy
import re

from jsonschema import Draft4Validator
from jsonschema.exceptions import SchemaError
from target_postgres.exceptions import JSONSchemaError

NULL = 'null'
OBJECT = 'object'
ARRAY = 'array'
INTEGER = 'integer'
NUMBER = 'number'
BOOLEAN = 'boolean'
STRING = 'string'
DATE_TIME_FORMAT = 'date-time'

_PYTHON_TYPE_TO_JSON_SCHEMA = {
    int: INTEGER,
    float: NUMBER,
    bool: BOOLEAN,
    str: STRING,
    type(None): NULL
}


def python_type(x):
    """
    Given a value `x`, return its Python Type as a JSONSchema type.
    :param x:
    :return:
    """
    if not type(x) in _PYTHON_TYPE_TO_JSON_SCHEMA:
        raise JSONSchemaError('Unknown type `{}`. Cannot translate to JSONSchema type.'.format(
            str(type(x))
        ))
    return _PYTHON_TYPE_TO_JSON_SCHEMA[type(x)]


def get_type(schema):
    """
    Given a JSON Schema dict, extracts the simplified `type` value
    :param schema: dict, JSON Schema
    :return: [string ...]
    """
    t = schema.get('type', None)
    if not t:
        return [OBJECT]

    if isinstance(t, str):
        return [t]

    return deepcopy(t)


def simple_type(schema):
    """
    Given a JSON Schema dict, extracts the simplified schema, ie, a schema which can only represent
    _one_ of the given types allowed (along with the Nullable modifier):
    - OBJECT
    - ARRAY
    - INTEGER
    - NUMBER
    - BOOLEAN
    - STRING
    - DATE_TIME

    :param schema: dict, JSON Schema
    :return: dict, JSON Schema
    """
    t = get_type(schema)

    if is_datetime(schema):
        return {'type': t,
                'format': DATE_TIME_FORMAT}

    return {'type': t}


def _get_ref(schema, paths):
    if not paths:
        return schema

    if not paths[0] in schema:
        raise JSONSchemaError('`$ref` "{}" not found in provided JSON Schema'.format(paths[0]))

    return _get_ref(schema[paths[0]], paths[1:])


def get_ref(schema, ref):
    """
    Given a JSON Schema dict, and a valid ref (`$ref`), get the JSON Schema from within schema
    :param schema: dict, JSON Schema
    :param ref: string
    :return: dict, JSON Schema
    :raises: Exception
    """

    # Explicitly only allow absolute internally defined $ref's
    if not re.match(r'^#/.*', ref):
        raise JSONSchemaError('Invalid format for `$ref`: "{}"'.format(ref))

    return _get_ref(schema,
                    re.split('/', re.sub(r'^#/', '', ref)))


def _is_ref(schema):
    """
    Given a JSON Schema compatible dict, returns True when the schema implements `$ref`

    NOTE: `$ref` OVERRIDES all other keys present in a schema
    :param schema:
    :return: Boolean
    """

    return '$ref' in schema


def _is_allof(schema):
    """
    Given a JSON Schema compatible dict, returns True when the schema implements `allOf`,
    AND has allOf elements.

    :param schema:
    :return: Boolean
    """

    return not _is_ref(schema) and 'allOf' in schema and schema['allOf']


def is_object(schema):
    """
    Given a JSON Schema compatible dict, returns True when schema's type allows being an Object.
    :param schema: dict, JSON Schema
    :return: Boolean
    """

    return not _is_ref(schema) \
           and (OBJECT in get_type(schema)
                or 'properties' in schema
                or not schema)


def is_iterable(schema):
    """
    Given a JSON Schema compatible dict, returns True when schema's type allows being iterable (ie, 'array')
    :param schema: dict, JSON Schema
    :return: Boolean
    """

    return not _is_ref(schema) \
           and ARRAY in get_type(schema) \
           and 'items' in schema


def is_nullable(schema):
    """
    Given a JSON Schema compatible dict, returns True when schema's type allows being 'null'
    :param schema: dict, JSON Schema
    :return: Boolean
    """

    return NULL in get_type(schema)


def is_literal(schema):
    """
    Given a JSON Schema compatible dict, returns True when schema's type allows being a literal
    (ie, 'integer', 'number', etc.)
    :param schema: dict, JSON Schema
    :return: Boolean
    """

    return not {STRING, INTEGER, NUMBER, BOOLEAN, NULL}.isdisjoint(set(get_type(schema)))


def is_datetime(schema):
    """
    Given a JSON Schema compatible dict, returns True when schema's type allows being a date-time
    :param schema: dict, JSON Schema
    :return: Boolean
    """

    return STRING in get_type(schema) and schema.get('format') == DATE_TIME_FORMAT


def make_nullable(schema):
    """
    Given a JSON Schema dict, returns the dict but makes the `type` `null`able.
    `is_nullable` will return true on the output.
    :return: dict, JSON Schema
    """
    t = get_type(schema)
    if NULL in t:
        return schema

    ret_schema = deepcopy(schema)
    ret_schema['type'] = t + [NULL]
    return ret_schema


def _allof_sort_key(schema):
    '''
    We prefer scalars over combinations.
    With scalars we prefer date-times over strings.
    With combinations, we prefer objects.
    With all, we prefer nullables.
    '''
    if is_nullable(schema):
        sort_value = 0
    else:
        sort_value = 1

    if is_datetime(schema):
        sort_value += 0
    elif is_literal(schema):
        sort_value += 10
    elif is_object(schema):
        sort_value += 100
    elif is_iterable(schema):
        sort_value += 200
    else:
        # Unknown schema...maybe a $ref?
        sort_value += 1000

    return sort_value


def _simplify__allof__merge__objects(schemas):
    ret_schema = schemas[0]
    # Merge objects together preferring later allOfs over earlier
    next_schemas = schemas[1:]
    while next_schemas and is_object(next_schemas[0]):
        ret_schema['properties'] = {
            **ret_schema['properties'],
            **next_schemas[0]['properties']}

        next_schemas = next_schemas[1:]

    return ret_schema


def _simplify__allof__merge__iterables(root_schema, schemas):
    ret_schema = schemas[0]
    # Recurse on all of the item schemas to create a single item schema
    item_schemas = []

    next_schemas = schemas
    while next_schemas and is_iterable(next_schemas[0]):
        item_schemas.append(next_schemas[0]['items'])

        next_schemas = next_schemas[1:]

    ret_schema['items'] = _helper_simplify(root_schema, {'allOf': item_schemas})
    return ret_schema


def _simplify__literals(root_schema, schema):
    '''
    Typically literals are simple and have at most two types, one of which being NULL.
    However, they _can_ have many types wrapped up inside them as an implicit `anyOf`.

    Since we support `anyOf`, it is simpler to unwrap and "flatten" this implicit
    combination type.
    '''
    schemas = []
    types = set(get_type(schema))
    types.discard(NULL)

    if is_datetime(schema):
        schemas.append({
            'type': [STRING],
            'format': DATE_TIME_FORMAT
        })
        types.remove(STRING)

    for t in get_type(schema):
        s = deepcopy(schema)
        s['type'] = [t]
        schemas.append(_helper_simplify(root_schema, s))
    
    if is_nullable(schema):
        schemas = [make_nullable(s) for s in schemas]
    
    if len(schemas) == 1:
        ret_schema = schemas[0]
    else:
        # TODO: merge/simplify anyOf schemas
        ret_schema = {'anyOf': schemas}


def _simplify__combinations(root_schema, schemas):
    simplified_schemas = [
        _helper_simplify(root_schema, schema)
        for schema in schemas]

    return sorted(simplified_schemas, key=_allof_sort_key)


def _helper_simplify(root_schema, child_schema):
    ## Refs override all other type definitions
    if _is_ref(child_schema):
        try:
            ret_schema = _helper_simplify(root_schema, get_ref(root_schema, child_schema['$ref']))
        except RecursionError:
            raise JSONSchemaError('`$ref` path "{}" is recursive'.format(get_ref(root_schema, child_schema['$ref'])))

    elif _is_allof(child_schema):
        schemas = _simplify__combinations(root_schema, child_schema['allOf'])

        ret_schema = schemas[0]

        if is_object(ret_schema):
            ret_schema = _simplify__allof__merge__objects(schemas)
        elif is_iterable(ret_schema):
            ret_schema = _simplify__allof__merge__iterables(root_schema, schemas)

    elif is_literal(child_schema):
        ret_schema = _simplify__literals(root_schema, child_schema)

    elif is_object(child_schema):
        properties = {}
        for field, field_json_schema in child_schema.get('properties', {}).items():
            properties[field] = _helper_simplify(root_schema, field_json_schema)

        ret_schema = {
            'type': [OBJECT],
            'properties': properties
        }

    elif is_iterable(child_schema):
        ret_schema = {
            'type': [ARRAY],
            'items': _helper_simplify(root_schema, child_schema.get('items', {}))
        }

    else:
        raise JSONSchemaError('Unsupported schema: {}'.format(child_schema))

    if 'format' in child_schema:
        ret_schema['format'] = child_schema.get('format')

    if 'default' in child_schema:
        ret_schema['default'] = child_schema.get('default')

    return ret_schema


def simplify(schema):
    """
    Given a JSON Schema compatible dict, returns a simplified JSON Schema dict

    - Expands `$ref` fields to their reference
    - Expands `type` fields into array'ed type fields
    - Strips out all fields which are not `type`/`properties`

    :param schema: dict, JSON Schema
    :return: dict, JSON Schema
    :raises: Exception
    """

    return _helper_simplify(schema, schema)


def _valid_schema_version(schema):
    return '$schema' not in schema \
           or schema['$schema'] == 'http://json-schema.org/draft-04/schema#'


def _unexpected_validation_error(errors, exception):
    """

    :param errors: [String, ...]
    :param exception: Exception
    :return: [String, ...]
    """

    if not errors:
        return ['Unexpected exception encountered: {}'.format(str(exception))]

    return errors


def validation_errors(schema):
    """
    Given a dict, returns any known JSON Schema validation errors. If there are none,
    implies that the dict is a valid JSON Schema.
    :param schema: dict
    :return: [String, ...]
    """

    errors = []

    if not isinstance(schema, dict):
        errors.append('Parameter `schema` is not a dict, instead found: {}'.format(type(schema)))

    try:
        if not _valid_schema_version(schema):
            errors.append('Schema version must be Draft 4. Found: {}'.format('$schema'))
    except Exception as ex:
        errors = _unexpected_validation_error(errors, ex)

    try:
        Draft4Validator.check_schema(schema)
    except SchemaError as error:
        errors.append(str(error))
    except Exception as ex:
        errors = _unexpected_validation_error(errors, ex)

    try:
        simplify(schema)
    except JSONSchemaError as error:
        errors.append(str(error))
    except Exception as ex:
        errors = _unexpected_validation_error(errors, ex)

    return errors


_shorthand_mapping = {
    NULL: '',
    'string': 's',
    'number': 'f',
    'integer': 'i',
    'boolean': 'b',
    'date-time': 't'
}


def _type_shorthand(type_s):
    if isinstance(type_s, list):
        shorthand = ''
        for t in sorted(type_s):
            shorthand += _type_shorthand(t)
        return shorthand

    if not type_s in _shorthand_mapping:
        raise JSONSchemaError('Shorthand not available for type {}. Expected one of {}'.format(
            type_s,
            list(_shorthand_mapping.keys())
        ))

    return _shorthand_mapping[type_s]


def shorthand(schema):
    t = deepcopy(get_type(schema))

    if 'format' in schema and 'date-time' == schema['format'] and STRING in t:
        t.remove(STRING)
        t.append('date-time')

    return _type_shorthand(t)
