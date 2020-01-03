# -*- coding:utf-8 -*-
from __future__ import absolute_import
import sys
sys.path.append("..")
import gym
import gym.spaces as spaces
import numpy as np
np.seterr(invalid='raise')
import pandas as pd
import csv
import uuid
import matplotlib.pyplot as plt
from pprint import pprint
from gym.utils import seeding
from empyrical import max_drawdown, calmar_ratio, omega_ratio, sharpe_ratio, sortino_ratio, downside_risk
from utils.globals import EPS, DATASETS_DIR, OUTPUTS_DIR, CAPITAL_BASE_MULTIPLIER, MAX_WEIGHT, RISK
from utils.enums import compute_indicators, lot_size, account_currency
from utils.data import read_h5_fx_history
from utils.math import my_round, sum_abs
from features.ta import get_indicators_returns


class PortfolioEnv(gym.Env):
    def __init__(self,
                 data_file='/datasets/price_history.h5',
                 output_file='/outputs/portfolio_management',
                 strategy_name='Strategy',
                 total_steps=1500,
                 window_length=7,
                 capital_base=1e6,
                 lot_size=lot_size.Standard,
                 leverage=0.01,
                 commission_percent=0.0,
                 commission_fixed=5,
                 max_slippage_percent=0.05,
                 start_date='2000-01-01',
                 end_date=None,
                 start_idx=None,
                 min_start_idx=0,
                 compute_indicators=compute_indicators.returns,  #   none  default  all   returns   ptr  rsi
                 add_noise=False,
                 debug = False
                 ):

        self.datafile = DATASETS_DIR + data_file
        
        if output_file is not None:
            self.output_file = OUTPUTS_DIR + output_file + '_' + (uuid.uuid4().hex)[:16] + '.csv'
        else:
            self.output_file = None
        
        self.strategy_name=strategy_name
        self.total_steps = total_steps
        self.capital_base = capital_base
        self.lot_size = lot_size.value
        self.leverage = leverage
        self.commission_percent = commission_percent / 100
        self.commission_fixed = commission_fixed
        self.max_slippage_percent = max_slippage_percent
        self.start_date = start_date
        self.end_date = end_date
        self.start_idx = start_idx
        self.min_start_idx = min_start_idx
        self.window_length = window_length
        self.compute_indicators = compute_indicators
        self.add_noise = add_noise
        self.debug = debug

        if add_noise == True:
            mu, sigma = 0, 0.1 
        else:
            mu, sigma = 0, 0

        self.instruments, self.price_history, self.tech_history = self._init_market_data()
        self.number_of_instruments = len(self.instruments)
        self.price_data, self.tech_data = self._get_episode_init_state()
        self.current_step = 0
        #   self.current_step = self.window_length - 1
        
        self.current_positions = np.zeros(len(self.instruments))
        self.current_portfolio_values = np.concatenate((np.zeros(len(self.instruments)), [self.capital_base]))   #   starting with cash only
        self.current_weights = np.concatenate((np.zeros(len(self.instruments)), [1.]))   #   starting with cash only
        #   self.current_date = self.tech_data.major_axis[self.current_step]
        
        self.portfolio_values = []
        self.returns = []
        self.log_returns = []
        self.positions = []
        self.weights = []
        self.trade_dates = []
        self.trade_steps = []
        self.infos = []

        self.done = (self.current_step >= self.total_steps) or (np.sum(self.current_portfolio_values) < CAPITAL_BASE_MULTIPLIER * self.capital_base)
            
        # openai gym attributes
        self.action_space = spaces.Box(-1, 1, shape=(len(self.instruments) + 1,), dtype=np.float32)
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(len(self.instruments), window_length, self.tech_data.shape[-1]), dtype=np.float32)
        
        self.noise = np.random.normal(mu, sigma, self.observation_space.shape) 
        self.action_sample = self.action_space.sample()

        self.seed()

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def reset(self):
        self.current_step = 0
        #   self.current_step = self.window_length - 1
        self.current_positions = np.zeros(len(self.instruments))
        self.current_portfolio_values = np.concatenate((np.zeros(len(self.instruments)), [self.capital_base]))   #   reset to cash only
        self.current_weights = np.concatenate((np.zeros(len(self.instruments)), [1.]))   #   reset to cash only
        self.price_data, self.tech_data = self._get_episode_init_state()
        self.done = (self.current_step >= self.total_steps) or (np.sum(self.current_portfolio_values) < CAPITAL_BASE_MULTIPLIER * self.capital_base)
        
        self.portfolio_values = []
        self.returns = []
        self.log_returns = []
        self.positions = []
        self.weights = []
        self.trade_dates = [] 
        self.trade_steps = []
        self.infos = []
                
        self.positions.append(self.current_positions)      
        self.portfolio_values.append(self.current_portfolio_values)
        self.weights.append(self.current_weights)
        self.trade_steps.append(self.current_step)
        
        return self._get_state()    #   , self.done
    
    def step(self, action):
        np.testing.assert_almost_equal(action.shape, (len(self.instruments) + 1,))

        # normalise just in case
        self.current_weights = np.clip(action, 0, 1)
        
        '''
        print(action)
        print(weights)
        '''
        
        self.current_weights /= (sum_abs(self.current_weights) + EPS)
        self.current_weights[-1] += np.clip(1 - sum_abs(self.current_weights), 0, 1)  # if weights are all zeros we normalise to [0,0..1]
        self.current_weights[-1] = sum_abs(self.current_weights[-1])
        
        if np.all(np.isnan(self.current_weights)):
            self.current_weights[:] = 0
            self.current_weights[-1] = 1
        
        np.testing.assert_almost_equal(sum_abs(self.current_weights), 1.0, 3, 'absolute weights should sum to 1. weights=%s' %self.current_weights)
       
        assert((self.current_weights >= 0) * (self.current_weights <= 1)).all(), 'all weights values should be between 0 and 1. Not %s' %self.current_weights
        
        self.weights.append(self.current_weights)
    
        self.current_step += 1

        current_prices = self.price_data[:, self.current_step + self.window_length - 1, 0]   #   open price
        slippage = np.random.uniform(0, self.max_slippage_percent/ 100, self.number_of_instruments) 
        #   slippage = self.max_slippage_percent/ 100   #   fixed slippage
        current_prices_with_slippage = np.multiply(current_prices, (1 + slippage)) 
        
        next_prices = self.price_data[:, self.current_step + self.window_length - 1, 3]   #   close price
        slippage = np.random.uniform(0, self.max_slippage_percent/ 100, self.number_of_instruments) 
        #   slippage = self.max_slippage_percent/ 100   #   fixed slippage
        next_prices_with_slippage = np.multiply(next_prices, (1 - slippage)) 

        self._rebalance(self.current_weights, current_prices_with_slippage)
        
        if self.current_weights[np.where(self.current_weights.any() > MAX_WEIGHT)] > 0:   #  or weights[-1] == 1:
            reward = -1
        else:
            reward = self._get_reward(current_prices_with_slippage, next_prices_with_slippage)

        pr, plr = self._get_portfolio_return()
        self.returns.append(pr)
        self.log_returns.append(plr)       
        #   reward = plr     
                   
        info = {
            'current step': self.current_step,
            'current prices': current_prices,
            'next prices': next_prices,  
            'current_weights': self.current_weights,        
            'portfolio value': np.sum(self.current_portfolio_values),
            'portfolio return':   pr,   
            'portfolio log return':   plr,   
            'reward': reward,
        }
        
        self.infos.append(info)
          
        self.done = (self.current_step >= self.total_steps) or (np.sum(self.current_portfolio_values) < CAPITAL_BASE_MULTIPLIER * self.capital_base)

        if self.done and self.output_file is not None:
            # Save infos to file
            keys = self.infos[0].keys()
            with open(self.output_file, 'w', newline='') as f:
                dict_writer = csv.DictWriter(f, keys)
                dict_writer.writeheader()
                dict_writer.writerows(self.infos)
        
        if self.debug == True:
            print('current step: {}, portfolio value: {}, portfolio return: {}, portfolio log return: {}, reward: {}'.format(self.current_step, my_round(np.sum(self.current_portfolio_values)), my_round(pr), my_round(plr), my_round(reward)))
            
        return self._get_state(), reward, self.done, info
    
    def render(self, mode='ansi', close=False):
        if close:
            return
        if mode == 'ansi':
            pprint(self.infos[-1], width=160, depth=2, compact=True)
        elif mode == 'human':
            self.plot()

    def plot(self):
        # show a plot of portfolio vs mean market performance
        df_info = pd.DataFrame(self.infos)
        df_info.set_index('current step', inplace=True)
        #   df_info.set_index('date', inplace=True)
        rn = np.asarray(df_info['portfolio return'])
        
        try:
            spf=df_info['portfolio value'].iloc[1]  #   Start portfolio value
            epf=df_info['portfolio value'].iloc[-1] #   End portfolio value
            pr = (epf-spf)/spf
        except :
            pr = 0
            
        try:
            sr = sharpe_ratio(rn)
        except :
            sr = 0
            
        try:
            sor = sortino_ratio(rn)
        except :
            sor = 0
            
        try:
            mdd = max_drawdown(rn)
        except :
            mdd = 0
            
        try:
            cr = calmar_ratio(rn)
        except :
            cr = 0
        
        try:
            om = omega_ratio(rn)
        except :
            om = 0
                        
        try:
            dr = downside_risk(rn)
        except :
            dr = 0

        print("First portfolio value: ", np.round(df_info['portfolio value'].iloc[1]))
        print("Last portfolio value: ", np.round(df_info['portfolio value'].iloc[-1]))
        
        title = self.strategy_name + ': ' + 'profit={: 2.2%} sharpe={: 2.2f} sortino={: 2.2f} max drawdown={: 2.2%} calmar={: 2.2f} omega={: 2.2f} downside risk={: 2.2f}'.format(pr, sr, sor, mdd, cr, om, dr)
        #   df_info[['market value', 'portfolio value']].plot(title=title, fig=plt.gcf(), figsize=(15,10), rot=30)
        df_info[['portfolio value']].plot(title=title, fig=plt.gcf(), figsize=(15,10), rot=30)
               
    def get_meta_state(self):
        return self.tech_data[:, self.current_step, :]

    def get_summary(self):
        portfolio_value_df = pd.DataFrame(np.array(self.portfolio_values), index=np.array(self.trade_steps), columns=self.instruments + ['cash'])
        positions_df = pd.DataFrame(np.array(self.positions), index=np.array(self.trade_steps), columns=self.instruments)
        weights_df = pd.DataFrame(np.array(self.weights), index=np.array(self.trade_steps), columns=self.instruments + ['cash'])
        return portfolio_value_df, positions_df, weights_df


    def _rebalance(self, weights, current_prices):
        target_weights = weights
        target_values = np.sum(self.current_portfolio_values) * target_weights
        
        #   target_positions = (1/self.leverage) * np.floor(target_values[:-1] / current_prices)
        #   target_positions = np.floor(target_values[:-1] / current_prices)
        
        current_margins = self.lot_size * self.leverage * current_prices * RISK
        #   pip_value = self._calculate_pip_value_in_account_currency(account_currency.USD, current_prices)
        #   current_margins = np.multiply(current_margins, pip_value)
        
        target_positions = np.floor(target_values[:-1] / current_margins)   
        
        trade_amount = target_positions - self.current_positions
        
        commission_cost = 0
        commission_cost += np.sum(self.commission_percent * np.abs(trade_amount) * current_prices)
        commission_cost += np.sum(self.commission_fixed * np.abs(trade_amount))
        self.current_weights = target_weights
        self.current_portfolio_values = target_values - commission_cost
        self.current_positions = target_positions
        #   self.current_date = self.preprocessed_market_data.major_axis[self.current_step]
        
        self.positions.append(self.current_positions)
        self.portfolio_values.append(self.current_portfolio_values)
        self.weights.append(self.current_weights) 
        self.trade_steps.append(self.current_step)
        #   self.trade_dates.append(self.current_date)
        
        if self.debug == True:
            print("----------------------------------------------")
            print("current_step: ", self.current_step)
            print("current_prices: ", current_prices)
            print("target_weights: ", my_round(target_weights))
            print("target_values: ", my_round(target_values))
            print("target_positions: ", my_round(target_positions))
            print("trade_amount: ", my_round(trade_amount))
            print("commission_cost: ", my_round(commission_cost))
            print("current_portfolio_values: ", my_round(self.current_portfolio_values))
            print("----------------------------------------------")   
            
    def _get_portfolio_return(self):
        cpv = np.sum(self.portfolio_values[-1])     # current portfolio value
        ppv = np.sum(self.portfolio_values[-2])     # previous portfolio value

        try:
            if ppv > 0:
                pr = (cpv-ppv)/ppv
            else:
                pr = 0
        except:
            pr = 0

        try:
            if pr > 0:
                plr =  np.log(pr) 
            else:
                plr = 0
        except:
            plr = 0 

        return pr, plr
    
    def _get_reward(self, current_prices, next_prices):
        #   returns_rate = next_prices / current_prices
        pip_value = self._calculate_pip_value_in_account_currency(account_currency.USD, next_prices)
        returns_rate = np.multiply(next_prices / current_prices, pip_value)
        log_returns = np.log(returns_rate)
        last_weight = self.current_weights
        securities_value = self.current_portfolio_values[:-1] * returns_rate
        self.current_portfolio_values[:-1] = securities_value
        self.current_weights = self.current_portfolio_values / np.sum(self.current_portfolio_values)
        reward = last_weight[:-1] * log_returns
                
        try:
            reward = reward.mean()
        except:
            reward = reward
        
        return reward

    def _get_episode_init_state(self):
        # get data for this episode, each episode might be different.
        if self.start_idx is None:
            self.idx = np.random.randint(low=self.min_start_idx, high=self.tech_history.shape[1] - self.total_steps)
        else:
            self.idx = self.start_idx
        
        assert self.idx >= self.window_length and self.idx <= self.tech_history.shape[1] - self.total_steps, 'Invalid start index'
        
        price_data = self.price_history[:, self.idx - self.window_length:self.idx + self.total_steps + 1, :]
        tech_data = self.tech_history[:, self.idx - self.window_length:self.idx + self.total_steps + 1, :]

        return price_data, tech_data

    def _get_state(self):
        tech_observation = self.tech_data[:, self.current_step:self.current_step + self.window_length, :]
        
        #   tech_observation = self.tech_data[:, self.current_step - self.window_length + 1:self.current_step + 1, :]
        
        return tech_observation

    def _get_normalized_state(self):
        data = self.tech_data.iloc[:, self.current_step + 1 - self.window_length:self.current_step + 1, :].values
        state = ((data - np.mean(data, axis=1, keepdims=True)) / (np.std(data, axis=1, keepdims=True) + EPS))[:, -1, :]
        return np.concatenate((state, self.current_weights[:-1][:, None]), axis=1)

    #   instruments = ['EUR/USD', 'USD/JPY', 'AUD/USD', 'USD/CAD',  'GBP/USD', 'NZD/USD', 'GBP/JPY', 'EUR/JPY', 'AUD/JPY', 'EUR/GBP', 'USD/CHF']
    def _calculate_pip_value_in_account_currency(self, currency, current_prices):
        pip_values = []
        #   print(type(current_prices))
        if currency == account_currency.USD:
            m = 0
            #   print(self.instruments)
            for instrument in self.instruments:
                #   print(instrument)
                if instrument == 'EUR/USD':
                    EUR_USD = current_prices[m]
                elif instrument == 'USD/JPY':
                    USD_JPY = current_prices[m]
                elif instrument == 'AUD/USD':
                    AUD_USD = current_prices[m]
                elif instrument == 'GBP/USD':
                    GBP_USD = current_prices[m]
                
                currency = instrument.split('/')
                first_currency = currency[0]
                second_currency = currency[1]
                
                if second_currency == 'USD':
                    pip_value = 0.0001
                elif first_currency == 'USD' and second_currency != 'JPY':
                    pip_value = 0.0001/current_prices[m]
                elif first_currency == 'USD' and second_currency == 'JPY':
                    pip_value = 0.01/current_prices[m]    
                elif instrument == 'GBP/JPY':
                    pip_value = GBP_USD * 0.01/current_prices[m] 
                elif instrument == 'EUR/JPY':
                    pip_value = EUR_USD * 0.01/current_prices[m] 
                elif instrument == 'AUD/JPY':
                    pip_value = AUD_USD * 0.01/current_prices[m] 
                elif instrument == 'EUR/GBP':
                    pip_value = EUR_USD * 0.0001/current_prices[m] 

                pip_values.append(pip_value)
                m += 1    

        return pip_values 
    
    '''
    def _calculate_pip_value_in_account_currency(self, currency, current_prices):
        pip_values = []
        currency = currency.split('/')
        first_currency = currency[0]
        second_currency = currency[1]

        current_price_currency = 0
        value_convert_to_usd = 0

        for x in range(len(current_prices)):
            if self.instruments[x] == currency:
                current_price_currency = current_prices[x]
                lotsizenow = self.lot_size[x]

        for x in range(len(self.instruments)):
            if second_currency != 'USD':
                convert_currency = first_currency + '/USD'
                if self.instruments[x] == convert_currency:
                    value_convert_to_usd = current_prices[x]

            #pip value multiplied by the bid/ask currency pair times the lot size = the price per pip
        if second_currency != 'JPY':    #if there is no JPY in the second currency the pip value is always 0.01% = 0.0001 of the current bid/ask
            pip_value = 0.0001 * current_price_currency * lotsizenow * 100000 * value_convert_to_usd
        elif second_currency == 'JPY':
            pip_value = 0.01 * current_price_currency * lotsizenow * 100000 * value_convert_to_usd #if the second currency is JPY the pip value is 1% = 0.01 of the current bid/ask

        pip_values.append(pip_value) #adds the pip value to an array and returns it

        return pip_values
    '''
    
    def _init_market_data(self):
        data, bid, ask, instruments = read_h5_fx_history(filepath=self.datafile, replace_zeros=True)
          
        if self.compute_indicators is compute_indicators.returns:
            new_data = np.zeros((0,0,0),dtype=np.float32)
            for i in range(data.shape[0]):
                security = pd.DataFrame(data[i, :, :]).fillna(method='ffill').fillna(method='bfill')
                security.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                tech_data = np.asarray(get_indicators_returns(security=security.astype(float), open_name='Open', high_name='High', low_name='Low', close_name='Close', volume_name='Volume'))
                new_data = np.resize(new_data, (new_data.shape[0]+1, tech_data.shape[0], tech_data.shape[1]))
                new_data[i] = tech_data    
            price_history = new_data[:,:,:5]
            tech_history = new_data[:,:,5:]
            
        #   print(price_history)    
        #   print(tech_history)    
            
        print('price_history has NaNs: {}, has infs: {}'.format(np.any(np.isnan(price_history)), np.any(np.isinf(price_history))))
        print('price_history shape: {}'.format(np.shape(price_history)))

        print('tech_history has NaNs: {}, has infs: {}'.format(np.any(np.isnan(tech_history)), np.any(np.isinf(tech_history))))
        print('tech_history shape: {}'.format(np.shape(tech_history)))
        
        assert np.sum(np.isnan(price_history)) == 0
        assert np.sum(np.isnan(tech_history)) == 0

        return instruments, price_history, tech_history