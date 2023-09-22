from collections import OrderedDict
from datetime import datetime
from importlib import import_module
import re


from django.conf import settings
from django.db import models
from django.db.models import Func, Count, Sum, Max, Min, Avg, Variance, StdDev
import pandas as pd

from .dates import Dates

USE_TZ = settings.USE_TZ

DELTA_REGEX = re.compile('^(?P<delta_int>-?\d*)(?P<delta_time>d{1}|m{1}|y{1})$')

CALC = {
    'avg': Avg,
    'sum': Sum,
    'count': Count,
    'min': Min,
    'max': Max,
    'variance': Variance,
    'std dev': StdDev
}


class Year(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y")"""
    output_field = models.CharField()


class Quarter(Func):
    template = """CONCAT(DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y"),
        " ",
        CEIL(EXTRACT(MONTH from %(expressions)s + INTERVAL %(interval)s SECOND)/3),
        "Q"
    )"""
    output_field = models.CharField()


class Month(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y-%%%%m")"""
    output_field = models.CharField()


class Day(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y-%%%%m-%%%%d")"""
    output_field = models.CharField()


class WeekDay(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%W")"""
    output_field = models.CharField()


class WeekDayHour(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%W %%%%H")"""
    output_field = models.CharField()


class Hour(Func):
    template = """DATE_FORMAT(
        CONVERT_TZ(
            %(expressions)s  + INTERVAL %(interval)s SECOND,
            "UTC",
            "%(tzinfo)s"
        ),
        "%%%%Y-%%%%m-%%%%d %%%%H")"""
    output_field = models.CharField()


EXTRACT = {
    'day': Day,
    'hour': Hour,
    'month': Month,
    'quarter': Quarter,
    'weekday': WeekDay,
    'weekdayhour': WeekDayHour,
    'year': Year
}


def get_model(model_name):
    module, _class_name = model_name.split('_')

    try:
        model = getattr(
            import_module('modules.{}.models'.format(module.lower())),
            _class_name
        )
    except ImportError:
        return

    return model


def get_fields(model, fields):
    return model._meta.get_fields()


async def aggregate(
    model, on_field, calc, timezone, distinct
):

    calc = CALC[calc[0]]

    aggregation = ''
    if isinstance(on_field, list):
        for key in on_field:
            if key in ['*', '+', '-', '/', '^']:
                aggregation += key
            else:
                aggregation += f'models.F("{key}")'
        aggregation = eval(aggregation)
    else:
        aggregation = on_field

    if on_field == 'id':
        data = await model.aaggregate(aggregated_total=calc(aggregation, distinct=distinct))

    elif isinstance(on_field, list):
        data = await model.aaggregate(
            aggregated_total=calc(
                aggregation,
                output_field=models.FloatField(),
                distinct=distinct
            )
        )

    elif on_field:
        data = await model.values(
            on_field
        ).aaggregate(aggregated_total=calc(aggregation, distinct=distinct))

    return {'total': data['aggregated_total']}


async def group_by(
    model, on_field, additional_fields, calc, groups, order,
    timezone, date_group, limit, distinct
):

    if not on_field:
        on_field = 'id'

    if not groups:
        groups = []

    if date_group:
        date_field = date_group['field']
        group_by = date_group['group_by']
        extracted = 'extracted_' + date_field
        truncate = EXTRACT[group_by.lower()]

        if USE_TZ:
            # Para ajustar a busca no db de acordo com o timezone
            seconds_offset = timezone.utcoffset(datetime.utcnow()).total_seconds()
            exp = {extracted: truncate(date_field, tzinfo=timezone, interval=seconds_offset)}
        else:
            exp = {extracted: truncate(date_field, tzinfo=timezone, interval=0)}
            # exp = {extracted: truncate(date_field, interval=0)}


        model = model.annotate(
            **exp
        )

        if order:
            order.insert(0, extracted)
        groups.insert(0, extracted)

    model = model.values(*groups)

    formulas = {}
    keys = []
    aggregation = ''

    if isinstance(on_field, list):
        for key in on_field:
            if key in ['*', '+', '-', '/', '^']:
                aggregation += key
            else:
                aggregation += f'models.F("{key}")'
        aggregation = eval(aggregation)
    else:
        aggregation = on_field

    for formula in calc:
        calc = CALC[formula]
        keys.append(formula)

        # Sum retorna Decimal e precisamos de Float/Int para exibir o gráfico corretamente
        if formula == 'sum':
            formulas[formula] = calc(
                aggregation,
                output_field=models.FloatField(),
                distinct=distinct
            )
        else:
            formulas[formula] = calc(aggregation, distinct=distinct)

    model = model.annotate(
        **formulas
    )

    if order:
        model = model.order_by(*order)

    if limit:
        model = model[0:limit]

    # Valores a serem retornados
    model = model.values(*groups, *keys, *additional_fields)

    results = []
    async for result in model:
        results.append(result)

    return results, groups


def get_dates(match, timezone):
    delta_int = match['delta_int']
    delta_time = match['delta_time']

    remove_tz = False if USE_TZ else True
    if delta_time == 'd':
        func = Dates(tz=timezone, remove_tz=remove_tz).day_delta

    elif delta_time == 'm':
        func = Dates(tz=timezone, remove_tz=remove_tz).month_delta

    elif delta_time == 'y':
        func = Dates(tz=timezone, remove_tz=remove_tz).year_delta

    return func(delta_int)


def get_period(period, timezone):
    if not period:
        return {}, None, None

    field = period.get('field')
    start_delta = period.get('start_delta')
    end_delta = period.get('end_delta')
    start_date = period.get('start_date')
    end_date = period.get('end_date')

    if not field and not (start_date or end_date or start_delta or end_delta):
        return {}, None, None

    date_filter = {}

    if start_date:
        date_filter[f'{field}__gte'] = start_date

    if end_date:
        date_filter[f'{field}__lte'] = end_date

    if start_delta:
        match = DELTA_REGEX.search(start_delta)
        if match:
            (start_date, _) = get_dates(match, timezone)
            date_filter[f'{field}__gte'] = start_date

    if end_delta:
        match = DELTA_REGEX.search(end_delta)
        if match:
            (_, end_date) = get_dates(match, timezone)
            date_filter[f'{field}__lte'] = end_date

    return [date_filter, start_date, end_date]


def normalize_dates(results, timezone, start_date, end_date, group_by, groups):
    if not results:
        return results

    start = results[0]
    end = results[-1]

    if not start_date:
        start_date = start['date']

    if not end_date:
        end_date = end['date']

    FREQ = {
        'hour': 'H',
        'day': 'D',
        'month': 'MS',
        'year': 'YS'
    }

    if USE_TZ:
        dates = pd.date_range(
            start_date, end_date, freq=FREQ[group_by.lower()], tz=timezone
        )
    else:
        dates = pd.date_range(
            start_date, end_date, freq=FREQ[group_by.lower()],
        )

    distinct = {}
    for result in results:
        for group in groups:
            if group not in distinct:
                distinct[group] = [result[group]]

            elif result[group] not in distinct[group]:
                distinct[group].append(result[group])

    data = {}
    for i in range(len(dates)):
        date = dates[i]
        data[date] = []

        for group in distinct:

            for item in distinct[group]:
                line = {}
                line['date'] = date
                line['y'] = 0
                line[group] = item


def normalize_groups(
    results, additional_fields, calc, timezone, start_date, end_date, group_by, groups
):
    if not results:
        return results

    keys = []
    values = []

    tmp = OrderedDict()

    x = groups[0]
    calc_key = calc[0]

    single_group = len(groups) == 1
    if not single_group:
        first_group = groups[1]

    # all_fields = calc + groups + additional_fields
    # print('Calc', calc)
    # print('Groups', groups)
    # print('Additional fields', additional_fields)
    # print('All fields', all_fields)
    # print('X', x)
    # print('Continue', single_group)
    # print('\n', results, '\n')

    for result in results:
        x_key = result[x]
        if x_key not in tmp:
            tmp[x_key] = {}

        for field in additional_fields:
            tmp[x_key][field] = result[field]

        if single_group:
            keys = calc
            for formula in calc:
                tmp[x_key][formula] = result[formula]

            continue

        # Agrupamentos com 2 ou mais itens só podem ter uma fórmula de cálculo
        second = str(result[first_group])
        if second not in tmp[x_key]:
            tmp[x_key][second] = result[calc_key]

        if second not in keys:
            keys.append(second)

    for x, results in tmp.items():
        result = {**results}
        if x:
            result['x'] = x
        elif x is None:
            result['x'] = 'Null'
        else:
            result['x'] = 'Empty'

        values.append(result)

    return values, keys


async def get_results(timezone, data):
    model = data['model']
    model = get_model(model)
    model = model.objects

    additional_fields = data.get('additional_fields', [])
    order = data.get('order', [])
    limit = data.get('limit')
    raw = data.get('raw')
    keys = data.get('keys')

    distinct = data.get('distinct', False)

    formulas = data.get('calc', {'formula': ['count'], 'field': 'id'})
    calc = formulas.get('formula', ['count'])

    on_field = formulas.get('field', 'id')

    groups = data.get('group_by', {})

    date_group = groups.get('date', {})
    groups = groups.get('fields', [])

    extra = data.get('extra')

    filter_by = data.get('filter_by', {})

    # Filtro por fields específicos
    filter_by_fields = filter_by.get('fields', {})
    model = model.filter(**filter_by_fields)

    # Filtro por um período específico
    filter_by_period = filter_by.get('period')
    start_date = end_date = None

    if filter_by_period:
        period_filter, start_date, end_date = get_period(filter_by_period, timezone.zone)
        model = model.filter(**period_filter)

    if extra:
        model = model.extra(**extra)

    if groups or date_group:
        results, groups = await group_by(
            model, on_field, additional_fields, calc, groups,
            order, timezone, date_group, limit, distinct
        )

        results_keys = []
        if results:
            if raw:
                results_keys = groups
            else:
                results, results_keys = normalize_groups(
                    results, additional_fields, calc, timezone, start_date, end_date,
                    date_group.get('group_by'), groups
                )

        if keys:
            new_keys = []
            new_results = []
            for key in results_keys:
                new_keys.append(keys[key])

            for result in results:
                new_result = {**result}
                for key, value in result.items():
                    if key in keys:
                        new_result[keys[key]] = value

                new_results.append(new_result)

            results = new_results
            results_keys = new_keys

        data = {
            'data': results,
            'keys': results_keys
        }

    else:
        data = await aggregate(
            model, on_field, calc, timezone, distinct
        )

    return data
