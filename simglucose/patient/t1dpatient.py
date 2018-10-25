from .base import Patient
import numpy as np
from scipy.integrate import ode
import pandas as pd
from collections import namedtuple
import logging
import pkg_resources

logger = logging.getLogger(__name__)

Action = namedtuple("patient_action", ['CHO', 'insulin'])
Observation = namedtuple("observation", ['Gsub'])

PATIENT_PARA_FILE = pkg_resources.resource_filename(
    'simglucose', 'params/vpatient_params.csv')


class T1DPatient(Patient):
    SAMPLE_TIME = 1  # min
    EAT_RATE = 5    # g/min CHO

    def __init__(self, params, init_state=None, t0=0):
        '''
        T1DPatient constructor.
        Inputs:
            - params: a pandas sequence
            - init_state: customized initial state.
              If not specified, load the default initial state in
              params.iloc[1:19]
            - t0: simulation start time, it is 0 by default
        '''
        self._params = params
        if init_state is None:
            init_state = self._params.iloc[1:19]
        self.init_state = init_state
        self.t0 = t0
        self.reset()

    @classmethod
    def withID(cls, patient_id, **kwargs):
        '''
        Construct patient by patient_id
        id are integers from 1 to 30.
        1  - 10: adolescent#001 - adolescent#010
        11 - 20: adult#001 - adult#001
        21 - 30: child#001 - child#010
        '''
        patient_params = pd.read_csv(PATIENT_PARA_FILE)
        params = patient_params.iloc[patient_id - 1, :]
        return cls(params, **kwargs)

    @classmethod
    def withName(cls, name, **kwargs):
        '''
        Construct patient by name.
        Names can be
            adolescent#001 - adolescent#010
            adult#001 - adult#001
            child#001 - child#010
        '''
        patient_params = pd.read_csv(PATIENT_PARA_FILE)
        params = patient_params.loc[patient_params.Name == name].squeeze()
        return cls(params, **kwargs)

    @property
    def state(self):
        return self._odesolver.y

    @property
    def t(self):
        return self._odesolver.t

    @property
    def sample_time(self):
        return self.SAMPLE_TIME

    def step(self, action):
        # Convert announcing meal to the meal amount to eat at the moment
        to_eat = self._announce_meal(action.CHO)
        action = action._replace(CHO=to_eat)

        # Detect eating or not and update last digestion amount
        if action.CHO > 0 and self._last_action.CHO <= 0:
            logger.info('t = {}, patient starts eating ...'.format(self.t))
            self._last_Qsto = self.state[0] + self.state[1]
            self._last_foodtaken = 0
            self.is_eating = True

        if to_eat > 0:
            # print(action.CHO)
            logger.debug('t = {}, patient eats {} g'.format(
                self.t, action.CHO))

        if self.is_eating:
            self._last_foodtaken += action.CHO   # g

        # Detect eating ended
        if action.CHO <= 0 and self._last_action.CHO > 0:
            logger.info('t = {}, Patient finishes eating!'.format(self.t))
            self.is_eating = False

        # Update last input
        self._last_action = action

        # ODE solver
        # print('Current simulation time: {}'.format(self.t))
        # print(self._last_Qsto)
        self._odesolver.set_f_params(
            action, self._params, self._last_Qsto, self._last_foodtaken)
        if self._odesolver.successful():
            self._odesolver.integrate(self._odesolver.t + self.sample_time)
        else:
            logger.error('ODE solver failed!!')
            raise

    @staticmethod
    def model(t, x, action, params, last_Qsto, last_foodtaken):
        dxdt = np.zeros(18)
        d = action.CHO * 1000  # g -> mg
        insulin = action.insulin * 6000 / params.BW  # U/min -> pmol/kg/min
        basal = params.u2ss * params.BW / 6000  # U/min

        # Glucose in the stomach
        qsto = x[0] + x[1]
        Dbar = last_Qsto + last_foodtaken

        # Stomach solid
        dxdt[0] = -params.kmax * x[0] + d

        if Dbar > 0:
            aa = 5 / 2 / (1 - params.b) / Dbar
            cc = 5 / 2 / params.d / Dbar
            kgut = params.kmin + (params.kmax - params.kmin) / 2 * (np.tanh(
                aa * (qsto - params.b * Dbar)) - np.tanh(cc * (qsto - params.d * Dbar)) + 2)
        else:
            kgut = params.kmax

        # stomach liquid
        dxdt[1] = params.kmax * x[0] - x[1] * kgut

        # intestine
        dxdt[2] = kgut * x[1] - params.kabs * x[2]

        # Rate of appearance
        Rat = params.f * params.kabs * x[2] / params.BW
        # Glucose Production
        EGPt = params.kp1 - params.kp2 * x[3] - params.kp3 * x[8] + params.kcounter * x[14]
        # Glucose Utilization
        Uiit = params.Fsnc

        # renal excretion
        if x[3] > params.ke2:
            Et = params.ke1 * (x[3] - params.ke2)  # equazione 27
        else:
            Et = 0

        # glucose kinetics
        # plus dextrose IV injection input u[2] if needed
        dxdt[3] = max(EGPt, 0) + Rat - Uiit - Et - \
            params.k1 * x[3] + params.k2 * x[4]
        dxdt[3] = (x[3] >= 0) * dxdt[3]
        Gt = x[3] / params.Vg

        if Gt < params.Gth:
            fG = np.log((params.Gth / params.Gb) ** params.r1)
        else:
            fG = np.log((Gt / params.Gb) ** params.r1)
        if Gt > params.Gb:
            risk = 0
        else:
            risk = 10 * fG**2
         # print(risk)


        Vmt = params.Vm0 + params.Vmx * x[6]
        Kmt = params.Km0
        # Uidt = Vmt * x[4] / (Kmt + x[4])
        Uidt = Vmt * (1 + params.r3 * risk) * x[4] / (Kmt + x[4])
        dxdt[4] = -Uidt + params.k1 * x[3] - params.k2 * x[4]
        dxdt[4] = (x[4] >= 0) * dxdt[4]

        # insulin kinetics
        dxdt[5] = -(params.m2 + params.m4) * x[5] + params.m1 * x[9] + params.ka1 * \
            x[10] + params.ka2 * x[11]  # plus insulin IV injection u[3] if needed
        It = x[5] / params.Vi
        dxdt[5] = (x[5] >= 0) * dxdt[5]

        # insulin action on glucose utilization
        dxdt[6] = -params.p2u * x[6] + params.p2u * (It - params.Ib)

        # insulin action on production
        dxdt[7] = -params.ki * (x[7] - It)

        dxdt[8] = -params.ki * (x[8] - x[7])

        # insulin in the liver (pmol/kg)
        dxdt[9] = -(params.m1 + params.m30) * x[9] + params.m2 * x[5]
        dxdt[9] = (x[9] >= 0) * dxdt[9]

        # subcutaneous insulin kinetics
        dxdt[10] = insulin - (params.ka1 + params.kd) * x[10]
        dxdt[10] = (x[10] >= 0) * dxdt[10]

        dxdt[11] = params.kd * x[10] - params.ka2 * x[11]
        dxdt[11] = (x[11] >= 0) * dxdt[11]

        # subcutaneous glcuose
        dxdt[12] = (-params.ksc * x[12] + params.ksc * x[3])
        dxdt[12] = (x[12] >= 0) * dxdt[12]

        # Glucagon kinetics and secretion
        SRd = params.kGSRd * max(-dxdt[3] / params.Vg, 0)
        SRt = x[15] + SRd
        #Rah = params.SQgluc_k2 * x[17]
        dxdt[13] = -params.k01g * x[13] + SRt
        dxdt[13] = (x[13] >= 0) * dxdt[13]

        # Endogenous glucose production
        dxdt[14] = -params.kXGn * x[14] + params.kXGn * max(x[13] - params.Gnb, 0)

        if It >= params.Ith:
            dxdt[15] = -params.r2 * (x[15] - max(params.kGSRs * (params.Gth - Gt) / (It - params.Ith + 1) + params.SRb , 0))
        else:
            dxdt[15] = -params.r2 * (x[15] - max(params.kGSRs * (params.Gth - Gt) + params.SRb , 0))


        dxdt[15] = (x[15] >= 0) * dxdt[15]


        # subcutaneous glucagon kinetic
        dxdt[16] = -(params.SQgluc_k1 + params.SQgluc_kc1) * x[16]
        dxdt[16] = (x[16] >= 0) * dxdt[16]
        dxdt[17] = params.SQgluc_k1 * x[16] -params.SQgluc_k2 * x[17]
        dxdt[17] = (x[17] >= 0) * dxdt[17]

        if action.insulin > basal:
            logger.debug('t = {}, injecting insulin: {}'.format(
                t, action.insulin))

        return dxdt

    @property
    def observation(self):
        '''
        return the observation from patient
        for now, only the subcutaneous glucose level is returned
        TODO: add heart rate as an observation
        '''
        GM = self.state[12]  # subcutaneous glucose (mg/dl)
        Gsub = GM / self._params.Vg
        observation = Observation(Gsub=Gsub)
        return observation

    def _announce_meal(self, meal):
        '''
        patient announces meal.
        The announced meal will be added to self.planned_meal
        The meal is consumed in self.EAT_RATE
        The function will return the amount to eat at current time
        '''
        self.planned_meal += meal
        if self.planned_meal > 0:
            to_eat = min(self.EAT_RATE, self.planned_meal)
            self.planned_meal -= to_eat
            self.planned_meal = max(0, self.planned_meal)
        else:
            to_eat = 0
        return to_eat

    def reset(self):
        '''
        Reset the patient state to default intial state
        '''
        self._last_Qsto = self.init_state[0] + self.init_state[1]
        self._last_foodtaken = 0
        self.name = self._params.Name

        self._odesolver = ode(self.model).set_integrator('dopri5')
        self._odesolver.set_initial_value(self.init_state, self.t0)

        self._last_action = Action(CHO=0, insulin=0)
        self.is_eating = False
        self.planned_meal = 0


if __name__ == '__main__':
    logger.setLevel(logging.INFO)
    # create console handler and set level to debug
    ch = logging.StreamHandler()
    # ch.setLevel(logging.DEBUG)
    ch.setLevel(logging.INFO)
    # create formatter
    formatter = logging.Formatter(
        '%(name)s: %(levelname)s: %(message)s')
    # add formatter to ch
    ch.setFormatter(formatter)
    # add ch to logger
    logger.addHandler(ch)

    p = T1DPatient.withName('adolescent#001')
    basal = p._params.u2ss * p._params.BW / 6000  # U/min
    t = []
    CHO = []
    insulin = []
    BG = []
    while p.t < 1000:
        ins = basal
        carb = 0
        if p.t == 100:
            carb = 80
            ins = 80.0 / 6.0 + basal
        # if p.t == 150:
        #     ins = 80.0 / 12.0 + basal
        act = Action(insulin=ins, CHO=carb)
        t.append(p.t)
        CHO.append(act.CHO)
        insulin.append(act.insulin)
        BG.append(p.observation.Gsub)
        p.step(act)

    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(3, sharex=True)
    ax[0].plot(t, BG)
    ax[1].plot(t, CHO)
    ax[2].plot(t, insulin)
    plt.show()
