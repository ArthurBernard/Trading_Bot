#!/usr/bin/env python3
# coding: utf-8
# @Author: ArthurBernard
# @Email: arthur.bernard.92@gmail.com
# @Date: 2020-03-17 12:23:25
# @Last modified by: ArthurBernard
# @Last modified time: 2020-03-18 08:34:59

""" A (very) light Graphical User Interface. """

# Built-in packages

# Third party packages
import fynance as fy

# Local packages


class ResultManager:
    """ Manager object of historical results of strategy.

    Attributes
    ----------
    df : pandas.DataFrame
        Data with each series to compute performances.
    period : int
        Maximal number of trading periods per year
    metrics : list of str
        List of metrics to compute performance. The following are available:
        'return', 'perf', 'sharpe', 'calmar', and 'mdd'.
    periods : list of str
        Frequency to compute performances. The following are available:
        'daily', 'weekly', 'monthly', 'yearly' and 'total'.

    Methods
    -------
    print_stats
    set_current_price
    set_current_value
    set_current_stats

    """

    def __init__(self, pnl, metrics=[], periods=[], period=252):
        """ Initialize object.

        Parameters
        ----------
        pnl : PnL object or pd.DataFrame
            Object to compute and store profit and loss of a trading bot.
        metrics : list of str
            List of metrics to display results. Is available 'return', 'perf',
            'sharpe', 'calmar' and 'maxdd'.
        periods : list of str
            List of periods to compte metrics. Is available 'daily', 'weekly',
            'monthly', 'yearly' and 'total'.
        period : int, optional
            Number of trading days per year, i.e 252 if trading on classical
            market and 364 for crypto-currencies market. Default is 252.

        """
        if isinstance(pnl, PnL):
            self.df = pnl.df
            self.period = period * 86400 / pnl.ts

        elif isinstance(pnl, pd.DataFrame):
            self.df = pnl
            self.period = period

        self.metrics = metrics
        self.periods = periods
        self.df = pnl.df
        self.logger = logging.getLogger(__name__)

    def set_current_price(self):
        """ Display current price and fees. """
        txt = 'Display results\n' + _set_text(
            ['-'],
            ['Price of the underlying: {:.2f}'.format(self.df.price.iloc[-1])],
            ['Current fees: {:.2}%'.format(self._get_current_fee())],
            ['-'],
        )

        return txt

    def set_current_value(self):
        """ Display the current share of portfolio in underlying and cash. """
        price = self.df.price.iloc[-1]
        value = self.df.value.iloc[-1]
        pos = self.df.position.iloc[-1]
        # TODO : fix problem with volume equal to 0. when pos is 0
        vol = self.df.volume.iloc[-1]
        txt = '\nCurrent value of the porfolio:\n'
        txt += _set_text(['-'] * 3, [
            'Portfolio',
            '{:.2f} $'.format(value),
            '{:.2f} ?'.format(value / price), ], [
            'Underlying part.',
            '{:.2f} $'.format(pos * vol * price),
            '{:.2%}'.format(pos), ], [
            'Base part',
            '{:.2f} $'.format((1 - pos) * vol * price),
            '{:.2%}'.format(1 - pos), ], ['-'] * 3)

        return txt

    def set_current_stats(self):
        """ Display some statistics for some time periods. """
        txt_table = [['-'] * (1 + len(self.metrics)), ['   '] + self.metrics]

        for period in self.periods:
            if period.lower() == 'daily':
                _index = self.df.index >= self.df.index[-1] - 86400

            elif period.lower() == 'weekly':
                _index = self.df.index >= self.df.index[-1] - 86400 * 7

            elif period.lower() == 'monthly':
                _index = self.df.index >= self.df.index[-1] - 86400 * 30

            elif period.lower() == 'yearly':
                _index = self.df.index >= self.df.index[-1] - 86400 * 365

            elif period.lower() == 'total':
                _index = self.df.index >= self.df.index[0]

            else:
                self.logger.error('Unknown period: {}'.format(period))
                continue

            txt_table += self._set_stats_result(self.df.loc[_index], period)

        txt_table += (['-'] * (1 + len(self.metrics)),)

        return '\nStatistics of results:\n' + _set_text(*txt_table)

    def print_stats(self):
        """ Print some statistics of historical results strategy. """
        txt = self.set_current_price()
        txt += self.set_current_value()
        txt += self.set_current_stats()

        # self.logger.info(txt)

        return txt

    def _set_stats_result(self, df, head):
        """ Set statistics in a table with header. """
        ui = df.price.values
        si = df.value.values

        return [
            ['-'] * (1 + len(self.metrics)),
            [head],
            ['Underlying'] + self.set_statistics(ui),
            ['Strategy'] + self.set_statistics(si),
        ]

    def set_statistics(self, series):
        """ Compute statistics of a series of price or index values.

        Parameters
        ----------
        series : np.ndarray[ndim=1, dtype=np.float64]
            Series of price or index values.

        Returns
        -------
        list
            Some statistics predefined when initialize the object.

        """
        metric_values = []
        for metric in self.metrics:
            if series.size < 2:
                metric_values += [0]

            elif metric.lower() == 'return':
                metric_values += [series[-1] - series[0]]

            elif metric.lower() in ['perf', 'perf.', 'performance']:
                metric_values += [series[-1] / series[0] - 1.]

            elif metric.lower() == 'sharpe':
                metric_values += [fy.sharpe(series, period=self.period)]

            elif metric.lower() == 'calmar':
                metric_values += [fy.calmar(series, period=self.period)]

            elif metric.lower() == 'maxdd':
                metric_values += [fy.mdd(series)]

            else:
                self.logger.error('Unknown metric: {}'.format(metric))

        return _rounder(*metric_values, dec=2)

    def _get_current_fee(self):
        fee = self.df.loc[self.df.fee != 0., 'fee'].values
        val = self.df.value.values
        if not fee.size:

            return 0.

        return 100 * fee[-1] / val[-1]


def _set_text(*args):
    """ Set a table. """
    n = max(len(arg) for arg in args)
    k_list = ['| ' if len(arg[0]) > 1 else '+' for arg in args]

    for i in range(n):
        i_args, n_spa, j = [], 0, 0

        for arg in args:
            if len(arg) >= i + 1:
                i_args += [arg]
                n_spa = max(n_spa, len(str(arg[i])))

        for arg in args:
            if len(arg[0]) > 1 and len(arg) >= i + 1:
                space = ' ' * (n_spa - len(str(arg[i])))
                k_list[j] += str(arg[i]) + space + ' | '

            elif len(arg[0]) == 1 and len(arg) >= i + 1:
                k_list[j] += arg[i] * (n_spa + 2) + '+'

            else:
                if i % 2 == 0:
                    k_list[j] = k_list[j][:-2] + ' ' * (n_spa + 3) + '| '

                else:
                    k_list[j] = '|' + ' ' * (n_spa + 3) + k_list[j][1:]

            j += 1

    return '\n'.join(k_list)


def _rounder(*args, dec=0):
    """ Round each element of a list. """
    return [round(float(arg), dec) for arg in args]


class GUI(_ClientGUI):
    pass


if __name__ == "__main__":

    gui = GUI()
    pass
