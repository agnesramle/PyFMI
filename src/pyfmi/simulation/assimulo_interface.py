#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2010 Modelon AB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
This file contains code for mapping our JMI Models to the Problem specifications
required by Assimulo.
"""
import logging

import numpy as N
import numpy.linalg as LIN
import pylab as P
import time

from pyfmi.common.io import ResultWriterDymola, ResultWriterDymola_deprecated
import pyfmi.fmi as fmi
import pyfmi.fmi_deprecated as fmi_deprecated
from pyfmi.common.core import TrajectoryLinearInterpolation

try:
    import assimulo
    assimulo_present = True
except:
    logging.warning(
        'Could not load Assimulo module. Check pyfmi.check_packages()')
    assimulo_present = False

if assimulo_present:
    from assimulo.problem import Implicit_Problem
    from assimulo.problem import Explicit_Problem
    from assimulo.exception import *

class FMIModel_Exception(Exception):
    """
    A FMIModel Exception.
    """
    pass

def write_data(simulator,write_scaled_result=False, result_file_name=''):
    """
    Writes simulation data to a file. Takes as input a simulated model.
    """
    #Determine the result file name
    if result_file_name == '':
        result_file_name=simulator.problem._model.get_name()+'_result.txt'

    model = simulator.problem._model

    t = N.array(simulator.problem._sol_time)
    r = N.array(simulator.problem._sol_real)
    data = N.c_[t,r]
    if len(simulator.problem._sol_int) > 0 and len(simulator.problem._sol_int[0]) > 0:
        i = N.array(simulator.problem._sol_int)
        data = N.c_[data,i]
    if len(simulator.problem._sol_bool) > 0 and len(simulator.problem._sol_bool[0]) > 0:
        #b = N.array(simulator.problem._sol_bool).reshape(
        #    -1,len(model._save_bool_variables_val))
        b = N.array(simulator.problem._sol_bool)
        data = N.c_[data,b]

    #export = ResultWriterDymola(model)
    export = ResultWriterDymola_deprecated(model) if (isinstance(model,fmi_deprecated.FMUModel) or isinstance(model,fmi_deprecated.FMUModel2)) else ResultWriterDymola(model)
    export.write_header(file_name=result_file_name)
    map(export.write_point,(row for row in data))
    export.write_finalize()
    #fmi.export_result_dymola(model, data)

def createLogger(model, minimum_level):
    """
    Creates a logger.
    """
    filename = model.get_name()+'.log'

    log = logging.getLogger(filename)
    log.setLevel(minimum_level)

    #ch = logging.StreamHandler()
    ch = logging.FileHandler(filename, mode='w', delay=True)
    ch.setLevel(0)

    formatter = logging.Formatter("%(name)s - %(message)s")

    ch.setFormatter(formatter)

    log.addHandler(ch)

    return log

class FMIODE(Explicit_Problem):
    """
    An Assimulo Explicit Model extended to FMI interface.
    """
    def __init__(self, model, input=None, result_file_name='',
                 with_jacobian=False, start_time=0.0, logging=False, result_handler=None):
        """
        Initialize the problem.
        """
        self._model = model
        self.input = input
        self.input_names = []

        #Set start time to the model
        self._model.time = start_time

        self.t0 = start_time
        self.y0 = self._model.continuous_states
        self.problem_name = self._model.get_name()

        [f_nbr, g_nbr] = self._model.get_ode_sizes()

        self._f_nbr = f_nbr
        self._g_nbr = g_nbr

        if g_nbr > 0:
            self.state_events = self.g
        self.time_events = self.t

        #If there is no state in the model, add a dummy
        #state der(y)=0
        if f_nbr == 0:
            self.y0 = N.array([0.0])

        #Determine the result file name
        if result_file_name == '':
            self.result_file_name = model.get_name()+'_result.txt'
        else:
            self.result_file_name = result_file_name
        self.debug_file_name = model.get_name().replace(".","_")+'_debug.txt'

        #Default values
        self.export = result_handler#ResultWriterDymola_deprecated(model) if (isinstance(model,fmi_deprecated.FMUModel) or isinstance(model,fmi_deprecated.FMUModel2)) else ResultWriterDymola(model)

        #Internal values
        self._sol_time = []
        self._sol_real = []
        self._sol_int  = []
        self._sol_bool = []
        self._logg_step_event = []
        self._write_header = True
        self._logging = logging

        #Stores the first time point
        #[r,i,b] = self._model.save_time_point()

        #self._sol_time += [self._model.t]
        #self._sol_real += [r]
        #self._sol_int  += [i]
        #self._sol_bool += b

        if with_jacobian:
            self.jac = self.j #Activates the jacobian

    def rhs(self, t, y, sw=None):
        """
        The rhs (right-hand-side) for an ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the rhs
        rhs = self._model.get_derivatives()

        #If there is no state, use the dummy
        if self._f_nbr == 0:
            rhs = N.array([0.0])

        return rhs

    def j(self, t, y, sw=None):
        """
        The jacobian function for an ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the jacobian

        #-Evaluating
        Jac = N.zeros(len(y)**2) #Matrix that holds the information

        #Compute Jac
        self._model.get_jacobian(1, 1, Jac)

        #-Vector manipulation
        Jac = Jac.reshape(len(y),len(y)).transpose() #Reshape to a matrix

        return Jac

    def g(self, t, y, sw):
        """
        The event indicator function for a ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the event indicators
        eventInd = self._model.get_event_indicators()

        return eventInd

    def t(self, t, y, sw):
        """
        Time event function.
        """
        eInfo = self._model.get_event_info()

        if eInfo.upcomingTimeEvent == True:
            return eInfo.nextEventTime
        else:
            return None


    def handle_result(self, solver, t, y):
        #
        #Post processing (stores the time points).
        #
        #Moving data to the model
        if t != self._model.time:
            #Moving data to the model
            self._model.time = t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0], self.input[1].eval(t)[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        self.export.integration_point()

    def handle_event(self, solver, event_info):
        """
        This method is called when Assimulo finds an event.
        """
        #Moving data to the model
        if solver.t!= self._model.time:
            self._model.time = solver.t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = solver.y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0],
                    self.input[1].eval(N.array([solver.t]))[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                f.write("\nDetected event at t = %.14E \n"%solver.t)
                f.write(" State event info: "+" ".join(str(i) for i in event_info[0])+ "\n")
                f.write(" Time  event info:  "+str(event_info[1])+ "\n")

                str_ind = ""
                for i in self._model.get_event_indicators():
                    str_ind += " %.14E"%i
                str_states = ""
                for i in solver.y:
                    str_states += " %.14E"%i
                str_der = ""
                for i in self._model.get_derivatives():
                    str_der += " %.14E"%i

        eInfo = self._model.get_event_info()
        eInfo.iterationConverged = False

        while eInfo.iterationConverged == False:
            self._model.event_update(intermediateResult=False)

            eInfo = self._model.get_event_info()
            #Retrieve solutions (if needed)
            #if eInfo.iterationConverged == False:
            #    pass

        #Check if the event affected the state values and if so sets them
        if eInfo.stateValuesChanged:
            if self._f_nbr == 0:
                solver.y[0] = 0.0
            else:
                solver.y = self._model.continuous_states

        #Get new nominal values.
        if eInfo.stateValueReferencesChanged:
            if self._f_nbr == 0:
                solver.atol = 0.01*solver.rtol*1
            else:
                solver.atol = 0.01*solver.rtol*self._model.nominal_continuous_states

        #Check if the simulation should be terminated
        if eInfo.terminateSimulation:
            raise TerminateSimulation #Exception from Assimulo

        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                str_ind2 = ""
                for i in self._model.get_event_indicators():
                    str_ind2 += " %.14E"%i
                str_states2 = ""
                for i in solver.y:
                    str_states2 += " %.14E"%i
                str_der2 = ""
                for i in self._model.get_derivatives():
                    str_der2 += " %.14E"%i

                f.write(" Indicators (pre) : "+str_ind + "\n")
                f.write(" Indicators (post): "+str_ind2+"\n")
                f.write(" States (pre) : "+str_states + "\n")
                f.write(" States (post): "+str_states2 + "\n")
                f.write(" Derivatives (pre) : "+str_der + "\n")
                f.write(" Derivatives (post): "+str_der2 + "\n\n")

                header = "Time (simulated) | Time (real) | "
                if solver.__class__.__name__=="CVode": #Only available for CVode
                    header += "Order | Error (Weighted)"
                f.write(header+"\n")

    def step_events(self, solver):
        """
        Method which is called at each successful step.
        """
        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                data_line = "%.14E"%solver.t+" | %.14E"%(solver.get_elapsed_step_time())
                #f.write(" Successful step at t = %.14E"%solver.t)
                #f.write(" Elapsed (real) time: %.14E"%(time.clock()-self._timer))

                if solver.__class__.__name__=="CVode": #Only available for CVode
                    #f.write(" Current order: "+str(solver.get_last_order()))
                    ele = solver.get_local_errors()
                    eweight = solver.get_error_weights()
                    err = ele*eweight
                    str_err = " |"
                    for i in err:
                        str_err += " %.14E"%i
                    #f.write(" Local (weighted) error vector:"+ str_err)
                    #f.write("\n")
                    data_line += " | %d"%solver.get_last_order()+str_err
                f.write(data_line+"\n")

        #Moving data to the model
        if solver.t != self._model.time:
            self._model.time = solver.t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = solver.y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0],
                    self.input[1].eval(N.array([solver.t]))[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        if self._model.completed_integrator_step():
            self._logg_step_event += [solver.t]
            #Event have been detect, call event iteration.
            #print "Step event detected at: ", solver.t
            #self.handle_event(solver,[0])
            return 1 #Tell to reinitiate the solver.
        else:
            return 0

    def print_step_info(self):
        """
        Prints the information about step events.
        """
        print '\nStep-event information:\n'
        for i in range(len(self._logg_step_event)):
            print 'Event at time: %e'%self._logg_step_event[i]
        print '\nNumber of events: ',len(self._logg_step_event)

    def initialize(self, solver):
        if self._logging:
            with open (self.debug_file_name, 'w') as f:
                model_valref = self._model.get_state_value_references()
                names = ""
                for i in model_valref:
                    names += self._model.get_variable_by_valueref(i) + ", "

                f.write("Solver: %s \n"%solver.__class__.__name__)
                f.write("State variables: "+names+ "\n")

                str_y = ""
                for i in solver.y:
                    str_y += " %.14E"%i

                f.write("Initial values: t = %.14E \n"%solver.t)
                f.write("Initial values: y ="+str_y+"\n\n")



                header = "Time (simulated) | Time (real) | "
                if solver.__class__.__name__=="CVode": #Only available for CVode
                    header += "Order | Error (Weighted)"
                f.write(header+"\n")

    def finalize(self, solver):
        self.export.simulation_end()

    def _set_input(self, input):
        self.__input = input

    def _get_input(self):
        return self.__input

    input = property(_get_input, _set_input, doc =
    """
    Property for accessing the input. The input must be a 2-tuple with the first
    object as a list of names of the input variables and with the other as a
    subclass of the class Trajectory.
    """)


class FMIODESENS(FMIODE):
    """
    FMIODE extended with sensitivity simulation capabilities
    """
    def __init__(self, model, input=None, result_file_name='',
                 with_jacobian=False, start_time=0.0, parameters=None, logging=False, result_handler=None):

        #Call FMIODE init method
        FMIODE.__init__(self, model, input, result_file_name, with_jacobian,
                start_time,logging,result_handler)

        #Store the parameters
        if parameters != None:
            if not isinstance(parameters,list):
                raise FMIModel_Exception("Parameters must be a list of names.")
            self.p0 = N.array(model.get(parameters)).flatten()
            self.pbar = N.array([N.abs(x) if N.abs(x) > 0 else 1.0 for x in self.p0])
        self.parameters = parameters


    def rhs(self, t, y, p=None, sw=None):
        #Sets the parameters, if any
        if self.parameters != None:
            self._model.set(self.parameters, p)

        return FMIODE.rhs(self,t,y,sw)


    def j(self, t, y, p=None, sw=None):

        #Sets the parameters, if any
        if self.parameters != None:
            self._model.set(self.parameters, p)

        return FMIODE.j(self,t,y,sw)

    def handle_result(self, solver, t, y):
        #
        #Post processing (stores the time points).
        #
        #Moving data to the model
        if t != self._model.time:
            #Moving data to the model
            self._model.time = t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0], self.input[1].eval(t)[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        #Sets the parameters, if any
        if self.parameters != None:
            p_data = N.array(solver.interpolate_sensitivity(t, 0)).flatten()

        self.export.integration_point(solver)#parameter_data=p_data)



class FMIODE_deprecated(Explicit_Problem):
    """
    An Assimulo Explicit Model extended to FMI interface.
    """
    def __init__(self, model, input=None, result_file_name='',
                 with_jacobian=False, start_time=0.0, logging=False):
        """
        Initialize the problem.
        """
        self._model = model
        self.input = input
        self.input_names = []

        #Set start time to the model
        self._model.time = start_time

        self.t0 = start_time
        self.y0 = self._model.continuous_states
        self.problem_name = self._model.get_name()

        [f_nbr, g_nbr] = self._model.get_ode_sizes()

        self._f_nbr = f_nbr
        self._g_nbr = g_nbr

        if g_nbr > 0:
            self.state_events = self.g
        self.time_events = self.t

        #If there is no state in the model, add a dummy
        #state der(y)=0
        if f_nbr == 0:
            self.y0 = N.array([0.0])

        #Determine the result file name
        if result_file_name == '':
            self.result_file_name = model.get_name()+'_result.txt'
        else:
            self.result_file_name = result_file_name
        self.debug_file_name = model.get_name().replace(".","_")+'_debug.txt'

        #Default values
        self.export = ResultWriterDymola_deprecated(model) if (isinstance(model,fmi_deprecated.FMUModel) or isinstance(model,fmi_deprecated.FMUModel2)) else ResultWriterDymola(model)

        #Internal values
        self._sol_time = []
        self._sol_real = []
        self._sol_int  = []
        self._sol_bool = []
        self._logg_step_event = []
        self._write_header = True
        self._logging = logging

        #Stores the first time point
        #[r,i,b] = self._model.save_time_point()

        #self._sol_time += [self._model.t]
        #self._sol_real += [r]
        #self._sol_int  += [i]
        #self._sol_bool += b

        if with_jacobian:
            self.jac = self.j #Activates the jacobian

    def rhs(self, t, y, sw=None):
        """
        The rhs (right-hand-side) for an ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the rhs
        rhs = self._model.get_derivatives()

        #If there is no state, use the dummy
        if self._f_nbr == 0:
            rhs = N.array([0.0])

        return rhs

    def j(self, t, y, sw=None):
        """
        The jacobian function for an ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the jacobian

        #-Evaluating
        Jac = N.zeros(len(y)**2) #Matrix that holds the information

        #Compute Jac
        self._model.get_jacobian(1, 1, Jac)

        #-Vector manipulation
        Jac = Jac.reshape(len(y),len(y)).transpose() #Reshape to a matrix

        return Jac

    def g(self, t, y, sw):
        """
        The event indicator function for a ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the event indicators
        eventInd = self._model.get_event_indicators()

        return eventInd

    def t(self, t, y, sw):
        """
        Time event function.
        """
        eInfo = self._model.get_event_info()

        if eInfo.upcomingTimeEvent == True:
            return eInfo.nextEventTime
        else:
            return None


    def handle_result(self, solver, t, y):
        #
        #Post processing (stores the time points).
        #
        #Moving data to the model
        if t != self._model.time:
            #Moving data to the model
            self._model.time = t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0], self.input[1].eval(t)[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()
        
        if solver.report_continuously:
            if self._write_header:
                self._write_header = False
                self.export.write_header(file_name=self.result_file_name)
            self.export.write_point()
        else:
            #Retrieves the time-point
            [r,i,b] = self._model.save_time_point()

            #Save the time-point
            self._sol_real += [r]
            self._sol_int  += [i]
            self._sol_bool += [b]
            self._sol_time += [t]

    def handle_event(self, solver, event_info):
        """
        This method is called when Assimulo finds an event.
        """
        #Moving data to the model
        if solver.t!= self._model.time:
            self._model.time = solver.t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = solver.y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0],
                    self.input[1].eval(N.array([solver.t]))[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                f.write("\nDetected event at t = %.14E \n"%solver.t)
                f.write(" State event info: "+" ".join(str(i) for i in event_info[0])+ "\n")
                f.write(" Time  event info:  "+str(event_info[1])+ "\n")

                str_ind = ""
                for i in self._model.get_event_indicators():
                    str_ind += " %.14E"%i
                str_states = ""
                for i in solver.y:
                    str_states += " %.14E"%i
                str_der = ""
                for i in self._model.get_derivatives():
                    str_der += " %.14E"%i

        eInfo = self._model.get_event_info()
        eInfo.iterationConverged = False

        while eInfo.iterationConverged == False:
            self._model.event_update(intermediateResult=False)

            eInfo = self._model.get_event_info()
            #Retrieve solutions (if needed)
            #if eInfo.iterationConverged == False:
            #    pass

        #Check if the event affected the state values and if so sets them
        if eInfo.stateValuesChanged:
            if self._f_nbr == 0:
                solver.y[0] = 0.0
            else:
                solver.y = self._model.continuous_states

        #Get new nominal values.
        if eInfo.stateValueReferencesChanged:
            if self._f_nbr == 0:
                solver.atol = 0.01*solver.rtol*1
            else:
                solver.atol = 0.01*solver.rtol*self._model.nominal_continuous_states

        #Check if the simulation should be terminated
        if eInfo.terminateSimulation:
            raise TerminateSimulation #Exception from Assimulo

        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                str_ind2 = ""
                for i in self._model.get_event_indicators():
                    str_ind2 += " %.14E"%i
                str_states2 = ""
                for i in solver.y:
                    str_states2 += " %.14E"%i
                str_der2 = ""
                for i in self._model.get_derivatives():
                    str_der2 += " %.14E"%i

                f.write(" Indicators (pre) : "+str_ind + "\n")
                f.write(" Indicators (post): "+str_ind2+"\n")
                f.write(" States (pre) : "+str_states + "\n")
                f.write(" States (post): "+str_states2 + "\n")
                f.write(" Derivatives (pre) : "+str_der + "\n")
                f.write(" Derivatives (post): "+str_der2 + "\n\n")

                header = "Time (simulated) | Time (real) | "
                if solver.__class__.__name__=="CVode": #Only available for CVode
                    header += "Order | Error (Weighted)"
                f.write(header+"\n")

    def step_events(self, solver):
        """
        Method which is called at each successful step.
        """
        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                data_line = "%.14E"%solver.t+" | %.14E"%(solver.get_elapsed_step_time())
                #f.write(" Successful step at t = %.14E"%solver.t)
                #f.write(" Elapsed (real) time: %.14E"%(time.clock()-self._timer))

                if solver.__class__.__name__=="CVode": #Only available for CVode
                    #f.write(" Current order: "+str(solver.get_last_order()))
                    ele = solver.get_local_errors()
                    eweight = solver.get_error_weights()
                    err = ele*eweight
                    str_err = " |"
                    for i in err:
                        str_err += " %.14E"%i
                    #f.write(" Local (weighted) error vector:"+ str_err)
                    #f.write("\n")
                    data_line += " | %d"%solver.get_last_order()+str_err
                f.write(data_line+"\n")

        #Moving data to the model
        if solver.t != self._model.time:
            self._model.time = solver.t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = solver.y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0],
                    self.input[1].eval(N.array([solver.t]))[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()
        
        if self._model.completed_integrator_step():
            self._logg_step_event += [solver.t]
            return 1 #Tell to reinitiate the solver.
        else:
            return 0

    def print_step_info(self):
        """
        Prints the information about step events.
        """
        print '\nStep-event information:\n'
        for i in range(len(self._logg_step_event)):
            print 'Event at time: %e'%self._logg_step_event[i]
        print '\nNumber of events: ',len(self._logg_step_event)

    def initialize(self, solver):
        if self._logging:
            with open (self.debug_file_name, 'w') as f:
                model_valref = self._model.get_state_value_references()
                names = ""
                for i in model_valref:
                    names += self._model.get_variable_by_valueref(i) + ", "

                f.write("Solver: %s \n"%solver.__class__.__name__)
                f.write("State variables: "+names+ "\n")

                str_y = ""
                for i in solver.y:
                    str_y += " %.14E"%i

                f.write("Initial values: t = %.14E \n"%solver.t)
                f.write("Initial values: y ="+str_y+"\n\n")



                header = "Time (simulated) | Time (real) | "
                if solver.__class__.__name__=="CVode": #Only available for CVode
                    header += "Order | Error (Weighted)"
                f.write(header+"\n")
    
    def finalize(self, solver):
        if solver.report_continuously:
            self.export.write_finalize()
        
    def _set_input(self, input):
        self.__input = input

    def _get_input(self):
        return self.__input

    input = property(_get_input, _set_input, doc =
    """
    Property for accessing the input. The input must be a 2-tuple with the first
    object as a list of names of the input variables and with the other as a
    subclass of the class Trajectory.
    """)






class FMIODE2(Explicit_Problem):
    """
    An Assimulo Explicit Model extended to FMI interface.
    """
    def __init__(self, model, input=None, result_file_name='',
                 with_jacobian=False, start_time=0.0, logging=False, result_handler=None):
        """
        Initialize the problem.
        """
        self._model = model
        self.input = input
        self.input_names = []

        #Set start time to the model
        self._model.time = start_time

        self.t0 = start_time
        self.y0 = self._model.continuous_states
        self.problem_name = self._model.get_name()

        [f_nbr, g_nbr] = self._model.get_ode_sizes()

        self._f_nbr = f_nbr
        self._g_nbr = g_nbr

        if g_nbr > 0:
            self.state_events = self.g
        self.time_events = self.t

        #If there is no state in the model, add a dummy
        #state der(y)=0
        if f_nbr == 0:
            self.y0 = N.array([0.0])

        #Determine the result file name
        if result_file_name == '':
            self.result_file_name = model.get_name()+'_result.txt'
        else:
            self.result_file_name = result_file_name
        self.debug_file_name = model.get_name().replace(".","_")+'_debug.txt'

        #Default values
        #self.export = ResultWriterDymola_deprecated(model) if (isinstance(model,fmi_deprecated.FMUModel) or isinstance(model,fmi_deprecated.FMUModel2)) else ResultWriterDymola(model)
        self.export = result_handler

        #Internal values
        self._sol_time = []
        self._sol_real = []
        self._sol_int  = []
        self._sol_bool = []
        self._logg_step_event = []
        self._write_header = True
        self._logging = logging

        #Stores the first time point
        #[r,i,b] = self._model.save_time_point()

        #self._sol_time += [self._model.t]
        #self._sol_real += [r]
        #self._sol_int  += [i]
        #self._sol_bool += b

        if self._model.get_capability_flags()['me_providesDirectionalDerivatives']:
            self.jac = self.j #Activates the jacobian

    def rhs(self, t, y, sw=None):
        """
        The rhs (right-hand-side) for an ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the rhs
        rhs = self._model.get_derivatives()

        #If there is no state, use the dummy
        if self._f_nbr == 0:
            rhs = N.array([0.0])

        return rhs

    def j(self, t, y, sw=None):
        """
        The jacobian function for an ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the jacobian

        #-Evaluating
        Jac = N.zeros(len(y)**2) #Matrix that holds the information, first y elements is the first column of the Jac

        #Compute Jac
        #self._model.get_jacobian(1, 1, Jac)

        states      = self._model.get_states_list()
        derivatives = self._model.get_derivatives_list()
        states_ref  = [s.value_reference for s in states.values()]
        deriv_ref   = [s.value_reference for s in derivatives.values()]
        v           = [0]*len(states_ref)

        for i in range(len(v)):
            v[i-1]=0
            v[i]=1
            Jac[i*len(v):(i+1)*len(v)] = self._model.get_directional_derivative(var_ref=states_ref, func_ref=deriv_ref, v=v)

        #-Vector manipulation
        Jac = Jac.reshape(len(y),len(y)).transpose() #Reshape to a matrix

        return Jac

    def g(self, t, y, sw):
        """
        The event indicator function for a ODE problem.
        """
        #Moving data to the model
        self._model.time = t
        #Check if there are any states
        if self._f_nbr != 0:
            self._model.continuous_states = y

        #Sets the inputs, if any
        if self.input!=None:
            self._model.set(self.input[0], self.input[1].eval(t)[0,:])

        #Evaluating the event indicators
        eventInd = self._model.get_event_indicators()

        return eventInd

    def t(self, t, y, sw):
        """
        Time event function.
        """
        eInfo = self._model.get_event_info()

        if eInfo.upcomingTimeEvent == True:
            return eInfo.nextEventTime
        else:
            return None


    def handle_result(self, solver, t, y):
        #
        #Post processing (stores the time points).
        #
        #Moving data to the model
        if t != self._model.time:
            #Moving data to the model
            self._model.time = t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0], self.input[1].eval(t)[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        self.export.integration_point()

    def handle_event(self, solver, event_info):
        """
        This method is called when Assimulo finds an event.
        """
        #Moving data to the model
        if solver.t!= self._model.time:
            self._model.time = solver.t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = solver.y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0],
                    self.input[1].eval(N.array([solver.t]))[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                f.write("\nDetected event at t = %.14E \n"%solver.t)
                f.write(" State event info: "+" ".join(str(i) for i in event_info[0])+ "\n")
                f.write(" Time  event info:  "+str(event_info[1])+ "\n")

                str_ind = ""
                for i in self._model.get_event_indicators():
                    str_ind += " %.14E"%i
                str_states = ""
                for i in solver.y:
                    str_states += " %.14E"%i
                str_der = ""
                for i in self._model.get_derivatives():
                    str_der += " %.14E"%i

        eInfo = self._model.get_event_info()
        eInfo.iterationConverged = False

        while eInfo.iterationConverged == False:
            self._model.event_update(intermediateResult=False)

            eInfo = self._model.get_event_info()
            #Retrieve solutions (if needed)
            #if eInfo.iterationConverged == False:
            #    pass

        #Call the Completed_event _iteration (New in FMI 2.0)
        self._model.completed_event_iteration()

        #Check if the event affected the state values and if so sets them
        if eInfo.stateValuesChanged:
            solver.y = self._model.continuous_states

        #Get new nominal values.
        if eInfo.stateValueReferencesChanged:
            solver.atol = 0.01*solver.rtol*self._model.nominal_continuous_states

        #Check if the simulation should be terminated
        if eInfo.terminateSimulation:
            raise TerminateSimulation #Exception from Assimulo

        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                str_ind2 = ""
                for i in self._model.get_event_indicators():
                    str_ind2 += " %.14E"%i
                str_states2 = ""
                for i in solver.y:
                    str_states2 += " %.14E"%i
                str_der2 = ""
                for i in self._model.get_derivatives():
                    str_der2 += " %.14E"%i

                f.write(" Indicators (pre) : "+str_ind + "\n")
                f.write(" Indicators (post): "+str_ind2+"\n")
                f.write(" States (pre) : "+str_states + "\n")
                f.write(" States (post): "+str_states2 + "\n")
                f.write(" Derivatives (pre) : "+str_der + "\n")
                f.write(" Derivatives (post): "+str_der2 + "\n\n")

                header = "Time (simulated) | Time (real) | "
                if solver.__class__.__name__=="CVode": #Only available for CVode
                    header += "Order | Error (Weighted)"
                f.write(header+"\n")

    def step_events(self, solver):
        """
        Method which is called at each successful step.
        """
        if self._logging:
            with open (self.debug_file_name, 'a') as f:
                data_line = "%.14E"%solver.t+" | %.14E"%(solver.get_elapsed_step_time())
                #f.write(" Successful step at t = %.14E"%solver.t)
                #f.write(" Elapsed (real) time: %.14E"%(time.clock()-self._timer))

                if solver.__class__.__name__=="CVode": #Only available for CVode
                    #f.write(" Current order: "+str(solver.get_last_order()))
                    ele = solver.get_local_errors()
                    eweight = solver.get_error_weights()
                    err = ele*eweight
                    str_err = " |"
                    for i in err:
                        str_err += " %.14E"%i
                    #f.write(" Local (weighted) error vector:"+ str_err)
                    #f.write("\n")
                    data_line += " | %d"%solver.get_last_order()+str_err
                f.write(data_line+"\n")

        #Moving data to the model
        if solver.t != self._model.time:
            self._model.time = solver.t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = solver.y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0],
                    self.input[1].eval(N.array([solver.t]))[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        if self._model.completed_integrator_step():
            self._logg_step_event += [solver.t]
            #Event have been detect, call event iteration.
            self.handle_event(solver,[0])
            return 1 #Tell to reinitiate the solver.
        else:
            return 0

    def print_step_info(self):
        """
        Prints the information about step events.
        """
        print '\nStep-event information:\n'
        for i in range(len(self._logg_step_event)):
            print 'Event at time: %e'%self._logg_step_event[i]
        print '\nNumber of events: ',len(self._logg_step_event)

    def initialize(self, solver):
        if self._logging:
            with open (self.debug_file_name, 'w') as f:
                model_valref = self._model.get_state_value_references()
                names = ""
                for i in model_valref:
                    names += self._model.get_variable_by_valueref(i) + ", "

                f.write("Solver: %s \n"%solver.__class__.__name__)
                f.write("State variables: "+names+ "\n")

                str_y = ""
                for i in solver.y:
                    str_y += " %.14E"%i

                f.write("Initial values: t = %.14E \n"%solver.t)
                f.write("Initial values: y ="+str_y+"\n\n")



                header = "Time (simulated) | Time (real) | "
                if solver.__class__.__name__=="CVode": #Only available for CVode
                    header += "Order | Error (Weighted)"
                f.write(header+"\n")

    def finalize(self, solver):
        self.export.simulation_end()

    def _set_input(self, input):
        self.__input = input

    def _get_input(self):
        return self.__input

    input = property(_get_input, _set_input, doc =
    """
    Property for accessing the input. The input must be a 2-tuple with the first
    object as a list of names of the input variables and with the other as a
    subclass of the class Trajectory.
    """)

class FMIODESENS2(FMIODE2):
    """
    FMIODE extended with sensitivity simulation capabilities
    """
    def __init__(self, model, input=None, result_file_name='',
                 with_jacobian=False, start_time=0.0, parameters=None, logging=False, result_handler=None):

        #Call FMIODE init method
        FMIODE.__init__(self, model, input, result_file_name, with_jacobian,
                start_time,logging, result_handler)

        #Store the parameters
        if parameters != None:
            if not isinstance(parameters,list):
                raise FMIModel_Exception("Parameters must be a list of names.")
            self.p0 = N.array(model.get(parameters)).flatten()
            self.pbar = N.array([N.abs(x) if N.abs(x) > 0 else 1.0 for x in self.p0])
        self.parameters = parameters


    def rhs(self, t, y, p=None, sw=None):
        #Sets the parameters, if any
        if self.parameters != None:
            self._model.set(self.parameters, p)

        return FMIODE.rhs(self,t,y,sw)


    def j(self, t, y, p=None, sw=None):

        #Sets the parameters, if any
        if self.parameters != None:
            self._model.set(self.parameters, p)

        return FMIODE2.j(self,t,y,sw)

    def handle_result(self, solver, t, y):
        #
        #Post processing (stores the time points).
        #
        #Moving data to the model
        if t != self._model.time:
            #Moving data to the model
            self._model.time = t
            #Check if there are any states
            if self._f_nbr != 0:
                self._model.continuous_states = y

            #Sets the inputs, if any
            if self.input!=None:
                self._model.set(self.input[0], self.input[1].eval(t)[0,:])

            #Evaluating the rhs (Have to evaluate the values in the model)
            rhs = self._model.get_derivatives()

        #Sets the parameters, if any
        if self.parameters != None:
            p_data = N.array(solver.interpolate_sensitivity(t, 0)).flatten()
        
        if solver.report_continuously:
            if self._write_header:
                self._write_header = False
                self.export.write_header(file_name=self.result_file_name, parameters=self.parameters)
            self.export.write_point(parameter_data=p_data)
        else:
            #Retrieves the time-point
            [r,i,b] = self._model.save_time_point()

            #Save the time-point
            self._sol_real += [r]
            self._sol_int  += [i]
            self._sol_bool += [b]
            self._sol_time += [t]




