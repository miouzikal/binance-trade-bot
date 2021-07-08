from collections import defaultdict
import random
import sys
import math
from datetime import datetime, timedelta

from typing import List

from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.expression import and_

from binance_trade_bot.auto_trader import AutoTrader
from binance_trade_bot.database import Pair, Coin, Trade

class Strategy(AutoTrader):
    def initialize(self):
        super().initialize()
        self.initialize_current_coin()
        self.reinit_threshold = self.manager.now().replace(second=0, microsecond=0)
        self.logger.info(f"CAUTION: The ratio_adjust strategy is still work in progress and can lead to losses! Use this strategy only if you know what you are doing, did alot of backtests and can live with possible losses.")

    def scout(self):
        #check if previous buy order failed. If so, bridge scout for a new coin.
        if self.failed_buy_order:
            self.bridge_scout()

        base_time: datetime = self.manager.now()
        allowed_idle_time = self.reinit_threshold
        if base_time >= allowed_idle_time:
            self.re_initialize_trade_thresholds()
            self.reinit_threshold = self.manager.now().replace(second=0, microsecond=0) + timedelta(minutes=1)

        """
        Scout for potential jumps from the current coin to another coin
        """
        current_coin = self.db.get_current_coin()
        # Display on the console, the current coin+Bridge, so users can see *some* activity and not think the bot has
        # stopped. Not logging though to reduce log size.
        # print(
        #     f"{self.manager.now()} - CONSOLE - INFO - I am scouting the best trades. "
        #     f"Current coin: {current_coin + self.config.BRIDGE} ",
        #     end="\r",
        # )

        current_coin_price = self.manager.get_sell_price(current_coin + self.config.BRIDGE)

        if current_coin_price is None:
            self.logger.info("Skipping scouting... current coin {} not found".format(current_coin + self.config.BRIDGE))
            return

        self._jump_to_best_coin(current_coin, current_coin_price)

    def bridge_scout(self):
        current_coin = self.db.get_current_coin()
        if self.manager.get_currency_balance(current_coin.symbol) > self.manager.get_min_notional(
            current_coin.symbol, self.config.BRIDGE.symbol
        ):
            # Only scout if we don't have enough of the current coin
            return
        new_coin = super().bridge_scout()
        if new_coin is not None:
            self.db.set_current_coin(new_coin)

    def _jump_to_best_coin(self, coin: Coin, coin_price: float, excluded_coins: List[Coin] = []):
        """
        Given a coin, search for a coin to jump to
        """
        ratio_dict, prices = self._get_ratios(coin, coin_price, excluded_coins)

        # keep only ratios bigger than zero
        ratio_dict = {k: v for k, v in ratio_dict.items() if v > 0}

        # if we have any viable options, pick the one with the biggest ratio
        if ratio_dict:
            if len(ratio_dict) > 1:
                pairs = sorted(ratio_dict.items(), key=lambda x: x[1], reverse=True)
            else:
                pairs = [max(ratio_dict, key=ratio_dict.get)]

            for pair in pairs:
                if isinstance(pair, tuple):
                    best_pair = pair[0]
                else:
                    best_pair = pair

                from_coin_balance = self.manager.get_currency_balance(best_pair.from_coin.symbol)
                from_coin_price = self.manager.get_ticker_price(best_pair.from_coin.symbol + self.config.BRIDGE.symbol)
                to_coin_price = self.manager.get_ticker_price(best_pair.to_coin.symbol + self.config.BRIDGE.symbol)

                session: Session
                with self.db.db_session() as session:
                    try:
                        trade = session.query(Trade).filter(Trade.alt_coin_id == best_pair.to_coin.symbol).filter(Trade.selling == False).order_by(Trade.datetime.desc()).limit(1).one().info()
                        if trade:
                           last_bought_amount = float(trade['alt_trade_amount'])
                    except:
                      last_bought_amount = 0

                if from_coin_balance is not None and from_coin_balance * from_coin_price > self.manager.get_min_notional(best_pair.from_coin.symbol, self.config.BRIDGE.symbol):
                    raw_bridge_balance = from_coin_balance * from_coin_price
                    bridge_balance = raw_bridge_balance - (raw_bridge_balance * 0.002)
                else:
                    bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)

                #print(f"STRATEGY: _buy_quantity({best_pair.from_coin.symbol}, {self.config.BRIDGE.symbol}, {bridge_balance}, {to_coin_price})")
                order_quantity = self.manager._buy_quantity(best_pair.from_coin.symbol, self.config.BRIDGE.symbol, bridge_balance, to_coin_price)
                if not float(order_quantity):
                    order_quantity = 0

                if last_bought_amount > 0:
                    pct_gain = ((order_quantity - last_bought_amount) / last_bought_amount)*100
                else:
                    pct_gain = 0
                if last_bought_amount > 0:
                    pct_gain = ((order_quantity - last_bought_amount) / last_bought_amount)*100
                else:
                    pct_gain = 0

                if order_quantity > last_bought_amount and (last_bought_amount == 0 or pct_gain > 2):
                    self.logger.info(f"Jump | {best_pair.from_coin.symbol} -> {best_pair.to_coin.symbol} | estimated gain : {round(pct_gain,2)}%")
                    self.transaction_through_bridge(best_pair, coin_price, prices[best_pair.to_coin_id], order_quantity)
                    break
                else:
                    #self.logger.info(f"Skip | {best_pair.from_coin.symbol} -> {best_pair.to_coin.symbol} | order : ({order_quantity}) / last trade : ({last_bought_amount})")
                    continue


    def initialize_current_coin(self):
        """
        Decide what is the current coin, and set it up in the DB.
        """
        if self.db.get_current_coin() is None:
            current_coin_symbol = self.config.CURRENT_COIN_SYMBOL
            if not current_coin_symbol:
                current_coin_symbol = random.choice(self.config.SUPPORTED_COIN_LIST)

            self.logger.info(f"Setting initial coin to {current_coin_symbol}")

            if current_coin_symbol not in self.config.SUPPORTED_COIN_LIST:
                sys.exit("***\nERROR!\nSince there is no backup file, a proper coin name must be provided at init\n***")
            self.db.set_current_coin(current_coin_symbol)

            # if we don't have a configuration, we selected a coin at random... Buy it so we can start trading.
            if self.config.CURRENT_COIN_SYMBOL == "":
                current_coin = self.db.get_current_coin()
                self.logger.info(f"Purchasing {current_coin} to begin trading")
                self.manager.buy_alt(
                    current_coin, self.config.BRIDGE, self.manager.get_buy_price(current_coin + self.config.BRIDGE)
                )
                self.logger.info("Ready to start trading")
            else:
                current_balance = self.manager.get_currency_balance(current_coin_symbol)
                sell_price = self.manager.get_sell_price(current_coin_symbol + self.config.BRIDGE.symbol)
                if current_balance is not None and current_balance * sell_price < self.manager.get_min_notional(current_coin_symbol, self.config.BRIDGE.symbol):
                    self.logger.info(f"Purchasing {current_coin_symbol} to begin trading")
                    current_coin = self.db.get_current_coin()
                    self.manager.buy_alt(
                        current_coin, self.config.BRIDGE, self.manager.get_buy_price(current_coin + self.config.BRIDGE)
                    )
                    self.logger.info("Ready to start trading")

    def re_initialize_trade_thresholds(self):
        """
        Re-initialize all the thresholds ( hard reset - as deleting db )
        """
        #updates all ratios
        #print('************INITIALIZING RATIOS**********')
        session: Session
        with self.db.db_session() as session:
            c1 = aliased(Coin)
            c2 = aliased(Coin)
            for pair in session.query(Pair).\
                join(c1, and_(Pair.from_coin_id == c1.symbol, c1.enabled == True)).\
                join(c2, and_(Pair.to_coin_id == c2.symbol, c2.enabled == True)).\
                all():
                if not pair.from_coin.enabled or not pair.to_coin.enabled:
                    continue
                #self.logger.debug(f"Initializing {pair.from_coin} vs {pair.to_coin}", False)

                from_coin_price = self.manager.get_sell_price(pair.from_coin + self.config.BRIDGE)
                if from_coin_price is None:
                    # self.logger.debug(
                    #     "Skipping initializing {}, symbol not found".format(pair.from_coin + self.config.BRIDGE),
                    #     False
                    # )
                    continue

                to_coin_price = self.manager.get_buy_price(pair.to_coin + self.config.BRIDGE)
                if to_coin_price is None:
                    # self.logger.debug(
                    #     "Skipping initializing {}, symbol not found".format(pair.to_coin + self.config.BRIDGE),
                    #     False
                    # )
                    continue

                pair.ratio = (pair.ratio *100 + from_coin_price / to_coin_price)  / (100 + 1)

    def initialize_trade_thresholds(self):
        """
        Initialize the buying threshold of all the coins for trading between them
        """
        session: Session
        with self.db.db_session() as session:
            pairs = session.query(Pair).filter(Pair.ratio.is_(None)).all()
            grouped_pairs = defaultdict(list)
            for pair in pairs:
                if pair.from_coin.enabled and pair.to_coin.enabled:
                    grouped_pairs[pair.from_coin.symbol].append(pair)

            price_history = {}
            base_date = self.manager.now().replace(second=0, microsecond=0)
            start_date = base_date - timedelta(minutes=100*2)
            end_date = base_date - timedelta(minutes=1)

            start_date_str = start_date.strftime('%Y-%m-%d %H:%M')
            end_date_str = end_date.strftime('%Y-%m-%d %H:%M')

            self.logger.info(f"Starting ratio init: Start Date: {start_date}, End Date {end_date}")
            for from_coin_symbol, group in grouped_pairs.items():

                if from_coin_symbol not in price_history.keys():
                    price_history[from_coin_symbol] = []
                    for result in  self.manager.binance_client.get_historical_klines(f"{from_coin_symbol}{self.config.BRIDGE_SYMBOL}", "1m", start_date_str, end_date_str, limit=100*2):
                        price = float(result[1])
                        price_history[from_coin_symbol].append(price)

                for pair in group:
                    to_coin_symbol = pair.to_coin.symbol
                    if to_coin_symbol not in price_history.keys():
                        price_history[to_coin_symbol] = []
                        for result in self.manager.binance_client.get_historical_klines(f"{to_coin_symbol}{self.config.BRIDGE_SYMBOL}", "1m", start_date_str, end_date_str, limit=100*2):
                           price = float(result[1])
                           price_history[to_coin_symbol].append(price)

                    if len(price_history[from_coin_symbol]) != 100*2:
                        self.logger.info(len(price_history[from_coin_symbol]))
                        self.logger.info(f"Skip initialization. Could not fetch last {100 * 2} prices for {from_coin_symbol}")
                        continue
                    if len(price_history[to_coin_symbol]) != 100*2:
                        self.logger.info(f"Skip initialization. Could not fetch last {100 * 2} prices for {to_coin_symbol}")
                        continue

                    sma_ratio = 0.0
                    for i in range(100):
                        sma_ratio += price_history[from_coin_symbol][i] / price_history[to_coin_symbol][i]
                    sma_ratio = sma_ratio / 100

                    cumulative_ratio = sma_ratio
                    for i in range(100, 100 * 2):
                        cumulative_ratio = (cumulative_ratio * 100 + price_history[from_coin_symbol][i] / price_history[to_coin_symbol][i]) / (100 + 1)

                    pair.ratio = cumulative_ratio

            self.logger.info(f"Finished ratio init...")

