import json
import torch.nn.functional as F
import torch
import matplotlib.pyplot as plt
import copy
import dynetwork
import gym
from gym import error
# from gym.utils import closer
import numpy as np
import networkx as nx
import math
import os
from our_agent import QAgent
import Packet
import random
import UpdateEdges as UE
from neural_network import NeuralNetwork
import matplotlib
import satellite_graphy
matplotlib.use('Agg')

'''Open file Setting.json which contains learning and network parameters. '''
main_dir = os.path.dirname(os.path.realpath(__file__))
main_dir = main_dir + '/'
with open(main_dir + 'Setting.json') as f:
  setting = json.load(f)



""" This class contains our gym environment which contains all of the necessary components for agents to take actions and receive rewards. file contains functions: 
    
    change_network: edge deletion/re-establish, edge weight change
    purgatory: queue to generate additional queues as previous packets are delivered
    step: obtain rewards for updating Q-table after an action
    is_capacity: check if next node is full and unable to receive packets
    send_packet: attempt to send packet to next node
    reset: reset environment after each episode
    resetForTest: reset environment for each trial (test for different networkloads)
    get_state: obtain packet's position info
    update_queues: update each nodes packet holding queue
    update_time: update packet delivery time
    calc_avg_delivery: helper function to calculate delivery time
    router: used to route all packets in ONE time stamp
    updateWhole: helper funciton update network environment and packets status
"""


class dynetworkEnv(gym.Env):

    '''Initialization of the network'''

    def __init__(self):
        self.nnodes = setting['NETWORK']['number nodes']
        self.nedges = setting['NETWORK']['edge degree']
        self.max_queue = setting['NETWORK']['holding capacity']
        self.max_transmit = setting['NETWORK']['sending capacity']
        self.npackets = setting['NETWORK']['initial num packets']
        self.max_initializations = setting['NETWORK']['max_additional_packets']
        self.max_edge_weight = setting['NETWORK']['max_edge_weight']
        self.min_edge_removal = setting['NETWORK']['min_edge_removal']
        self.max_edge_removal = setting['NETWORK']['max_edge_removal']
        self.edge_change_type = setting['NETWORK']['edge_change_type']
        self.network_type = setting['NETWORK']['network_type']
        self.initial_dynetwork = None
        self.dynetwork = None
        self.packet = -1
        self.curr_queue = []
        self.remaining = []
        self.nodes_traversed = 0
        self.print_edge_weights = True

        """ below we create a dynetwork type object """
        self.input_q_size = setting['DQN']['take_queue_size_as_input']
        self.dqn = self.init_dqns()
        if self.network_type == 'barabasi-albert':
            #network = nx.barabasi_albert_graph(self.nnodes, self.nedges)
            satellite_net = satellite_graphy.Network_input()
            network = satellite_net.graphy_generate()
        else:
            network = nx.gnm_random_graph(self.nnodes, self.nedges)
        receiving_queue_dict, sending_queue_dict = {}, {}
        for i in range(self.nnodes):
            temp_receiving_queue = {'receiving_queue': []}
            temp_sending_queue = {'sending_queue': []}
            receiving_queue_dict.update({i: temp_receiving_queue})
            sending_queue_dict.update({i: temp_sending_queue})
        del temp_receiving_queue, temp_sending_queue
        '''Attribute added'''
        """ node attributes """
        nx.set_node_attributes(network, copy.deepcopy(
            self.max_transmit), 'max_send_capacity')
        nx.set_node_attributes(network, copy.deepcopy(
            self.max_queue), 'max_receive_capacity')
        nx.set_node_attributes(network, copy.deepcopy(
            self.max_queue), 'congestion_measure')
        nx.set_node_attributes(network, receiving_queue_dict)
        nx.set_node_attributes(network, sending_queue_dict)
        """ edge attributes """
        nx.set_edge_attributes(network, 0, 'num_traversed')
        nx.set_edge_attributes(network, 0, 'edge_delay')
        nx.set_edge_attributes(network, 0, 'sine_state')
        """ CongestM Attribute added """
        nx.set_node_attributes(network, 0, 'max_queue_len')
        nx.set_node_attributes(network, 0, 'avg_q_len_array')
        nx.set_node_attributes(network, 0, 'growth')
        """ max_weight for edges """
        for s_edge, e_edge in network.edges:
            network[s_edge][e_edge]['edge_delay'] = random.randint(0, self.max_edge_weight)
            network[s_edge][e_edge]['sine_state'] = random.uniform(0, math.pi)
            network[s_edge][e_edge]['initial_weight'] = network[s_edge][e_edge]['edge_delay']
        """ make a copy so that we can preserve the initial state of the network """
        self.initial_dynetwork = dynetwork.DynamicNetwork(
            copy.deepcopy(network), self.max_initializations)
        """ here, we save the initial network as a .gexf file  """
        script_dir = os.path.dirname(__file__)
        results_dir = os.path.join(script_dir, 'q-learning/')
        if not os.path.isdir(results_dir):
            os.makedirs(results_dir)
        nx.write_gexf(network, results_dir + "graph.gexf")
        self.dynetwork = copy.deepcopy(self.initial_dynetwork)
        
        """ use dynetwork class method randomGeneratePackets to populate the network with packets """ 
        self.dynetwork.randomGeneratePackets(copy.deepcopy(self.npackets))
        self._positions = nx.spring_layout(self.dynetwork._network)

    ''' Function to handle routing all the packets in one time step. 
    Set will_learn to True if we are training and wish to update the Q-table; 
    else if we are testing set will_learn = False. '''
    def router(self, agent, will_learn=True):
        """ router attempts to route as many packets as the network will allow """
        node_queue_lengths = [0]
        num_nodes_at_capacity = 0
        num_nonEmpty_nodes = 0
        for nodeIdx in self.dynetwork._network.nodes:
            """ the self.nodes_traversed tracks the number of nodes we have looped over, 
            guaranteeing that each packet will have the same epsilon at each time step"""
            self.nodes_traversed += 1
            if self.nodes_traversed == self.nnodes:
                agent.config['update_epsilon'] = True
                self.nodes_traversed = 0
            node = self.dynetwork._network.nodes[nodeIdx]
            """ provides pointer for queue of current node """
            self.curr_queue = node['sending_queue']
            sending_capacity = node['max_send_capacity']
            queue_size = len(self.curr_queue)

            """ Congestion Measure #1: maximum queue lengths """
            if(queue_size > self.dynetwork._max_queue_length):
                self.dynetwork._max_queue_length = queue_size

            """ Congestion Measure #2: avg queue len pt1 """
            if(queue_size > 0):
                node_queue_lengths.append(queue_size)
                num_nonEmpty_nodes += 1
                """ Congestion Measure #3: avg percent at capacity """
                if(queue_size > sending_capacity):
                    """ increment number of nodes that are at capacity """
                    num_nodes_at_capacity += 1

            """ stores packets which currently have no destination path """
            self.remaining = []
            sendctr = 0
            for i in range(queue_size):
                """ when node cannot send anymore packets break and move to next node """
                if sendctr == sending_capacity:
                    self.dynetwork._rejections += (1 *
                                                   (len(node['sending_queue'])))
                    break
                self.packet = self.curr_queue[0]
                pkt_state = self.get_state(self.packet)
                nlist = sorted(
                    list(self.dynetwork._network.neighbors(pkt_state[0])))
                cur_state = F.one_hot(torch.tensor(
                    [pkt_state[0]]), self.nnodes)

                """ whether or not we input nodes' queue_size to the network """
                if (self.input_q_size):
                    cur_size = torch.tensor(
                        [len(self.curr_queue)]).unsqueeze(0)
                    cur_state = torch.cat((cur_state, cur_size), dim=1)

                action = agent.act(self.dqn[pkt_state[1]], cur_state, nlist)
                reward, self.remaining, self.curr_queue, action = self.step(
                    action, pkt_state[0])

                if reward != None:
                    sendctr += 1
                if will_learn:
                    if action != None:
                        next_state = F.one_hot(
                            torch.tensor([action]), self.nnodes)
                        """ whether or not we input nodes' queue_size to the network """
                        if (self.input_q_size):
                            next_size = len(
                                self.dynetwork._network.nodes[action]['sending_queue'])
                            next_size_tensor = torch.tensor(
                                [next_size]).unsqueeze(0)
                            next_state = torch.cat(
                                (next_state, next_size_tensor), dim=1).float()

                        """ self.dqn[pkt_State[1]] is the neural network of the packet's destination node """
                        agent.learn(self.dqn[pkt_state[1]],
                                    cur_state, action, reward, next_state)

            node['sending_queue'] = self.remaining + node['sending_queue']

        """ Congestion Measure #2: avg queue len pt2 """
        if len(node_queue_lengths) > 1:
            self.dynetwork._avg_q_len_arr.append(
                np.average(node_queue_lengths[1:]))  
        """ Congestion Measure #3: percent node at capacity """
        self.dynetwork._num_capacity_node.append(num_nodes_at_capacity)

        self.dynetwork._num_working_node.append(num_nonEmpty_nodes)

        """ Congestion Mesure #4: percent empty nodes """
        self.dynetwork._num_empty_node.append(
            self.dynetwork.num_nodes - num_nonEmpty_nodes)

    '''helper function to update learning enviornment in each time stamp'''
    def updateWhole(self, agent, learn=True):
        self.change_network()
        self.purgatory()
        self.update_queues()
        self.update_time()
        ''' this will iterate through every packet, throughout all nodes and make them hop over to the next node '''
        self.router(agent, learn)

    '''Use to update edges in network'''
    def change_network(self):
        UE.Delete(self.dynetwork, self.min_edge_removal, self.max_edge_removal)
        UE.Restore(self.dynetwork)
        if self.edge_change_type == 'none':
            pass
        elif self.edge_change_type == 'sinusoidal':
            UE.Sinusoidal(self.dynetwork)
        else:
            UE.Random_Walk(self.dynetwork)

    def purgatory(self):
        temp_purgatory = copy.deepcopy(self.dynetwork._purgatory)
        self.dynetwork._purgatory = []
        for (index, weight) in temp_purgatory:
            self.dynetwork.GeneratePacket(index, weight)

    ''' Takes packets which are now ready to be sent and puts them in the sending queue of the node '''
    def update_queues(self):
        for nodeIdx in self.dynetwork._network.nodes:
            node = self.dynetwork._network.nodes[nodeIdx]
            node['growth'] = len(node['receiving_queue'])
            receiving_queue = copy.deepcopy(node['receiving_queue'])
            for elt in receiving_queue:
                ''' increment packet delivery time stamp '''
                pkt = elt[0]
                if elt[1] == 0:
                    node['sending_queue'].append(pkt)
                    node['receiving_queue'].remove(elt)
                else:
                    idx = node['receiving_queue'].index(elt)
                    node['receiving_queue'][idx] = (pkt, elt[1] - 1)

    ''' Update time spent in queues for each packets '''
    def update_time(self):
        for nodeIdx in self.dynetwork._network.nodes:
            for elt in self.dynetwork._network.nodes[nodeIdx]['receiving_queue']:
                ''' increment packet delivery time stamp '''
                pkt = elt[0]
                curr_time = self.dynetwork._packets.packetList[pkt].get_time()
                self.dynetwork._packets.packetList[pkt].set_time(curr_time + 1)
            for c_pkt in self.dynetwork._network.nodes[nodeIdx]['sending_queue']:
                curr_time = self.dynetwork._packets.packetList[c_pkt].get_time(
                )
                self.dynetwork._packets.packetList[c_pkt].set_time(
                    curr_time + 1)

    ''' given an neighboring node (action), will check if node has a available space in that queue. if it does not, the packet stays at current queue. else, packet is sent to action node's queue. '''
    def step(self, action, curNode=None):
        reward = None
        ''' checks if action is None, in which case current node has no neighbors and also checks to see if target node has space in queue '''
        if (action == None) or (self.is_capacity(self.dynetwork, action)):
            self.curr_queue.remove(self.packet)
            self.remaining.append(self.packet)
            self.dynetwork._rejections += 1
        else:
            reward = self.send_packet(action)
        return reward, self.remaining, self.curr_queue, action

    """ checks to see if there is space in target_nodes queue """
    def is_capacity(self, dynetwork, target_node):
        total_queue_len = len(dynetwork._network.nodes[target_node]['sending_queue']) + \
            len(dynetwork._network.nodes[target_node]['receiving_queue'])
        return total_queue_len >= dynetwork._network.nodes[target_node]['max_receive_capacity']

    ''' Given next_step, send packet to next_step. Check if the node is full/other considerations beforehand. '''
    def send_packet(self, next_step):
        reward = 0
        pkt = self.dynetwork._packets.packetList[self.packet]
        curr_node = pkt.get_curPos()
        dest_node = pkt.get_endPos()
        weight = self.dynetwork._network[curr_node][next_step]['edge_delay']
        pkt.set_curPos(next_step)
        self.dynetwork._packets.packetList[self.packet].set_time(
            pkt.get_time() + weight)
        if pkt.get_curPos() == dest_node:
            """ if packet has reached destination, a new packet is created with the same 'ID' (packet index) but a new destination, which is then redistributed to another node """
            self.dynetwork._delivery_times.append(
                self.dynetwork._packets.packetList[self.packet].get_time())
            self.dynetwork._deliveries += 1
            self.dynetwork.GeneratePacket(self.packet, random.randint(0, 5))
            self.curr_queue.remove(self.packet)
            reward = 20*self.nnodes
        else:
            self.curr_queue.remove(self.packet)
            try:
                """ we reward the packet for being sent to a node according to our current reward function """
                q = len(self.dynetwork._network.nodes[next_step]['sending_queue']) + len(self.dynetwork._network.nodes[next_step]['receiving_queue'])
                q_eq = 0.8*self.max_queue
                w = 5
                growth = self.dynetwork._network.nodes[next_step]['growth']
                reward = (-(q-q_eq+w*growth))
            except nx.NetworkXNoPath:
                """ if the node the packet was just sent to has no available path to dest_node, we assign a reward of -50 """
                reward = -50
            self.dynetwork._network.nodes[next_step]['receiving_queue'].append(
                (self.packet, weight))

        return reward

    """ this function resets the environment """
    def reset(self, curLoad=None):
        self.dynetwork = copy.deepcopy(self.initial_dynetwork)
        if curLoad != None:
            self.npackets = curLoad
        self.dynetwork.randomGeneratePackets(self.npackets)
        print('Environment reset')

    ''' return packet's position and destinition'''
    def get_state(self, pktIdx):
        pkt = self.dynetwork._packets.packetList[self.packet]
        return (pkt.get_curPos(), pkt.get_endPos())

    '''helper function to calculate delivery times'''
    def calc_avg_delivery(self):
        ''' sum of avg deliveries / num packets delivered  '''
        avg = sum(self.dynetwork._delivery_times) / \
            len(self.dynetwork._delivery_times)
        print("avg deliv time:", avg)
        return avg

    '''Initialize all neural networks with one neural network initialized for each node in the network.'''
    def init_dqns(self):
        temp_dqns = []
        for i in range(self.nnodes):
            temp_dqn = NeuralNetwork(i, self.nnodes, self.input_q_size)
            temp_dqns.append(temp_dqn)
        return temp_dqns

    '''Update the target neural network to match the policy neural network'''
    def update_target_weights(self):
        for nn in self.dqn:
            nn.target_net.load_state_dict(nn.policy_net.state_dict())

    
    def helper_calc_reward(self):
        state = F.one_hot(torch.tensor([0]), self.nnodes)

        if(self.input_q_size):
            print("Take_Queue_Size_As_Additional_Input")
            size = torch.tensor([5]).unsqueeze(0)
            state = torch.cat((state, size), dim=1)

        #past_reward = self.dqn[1].policy_net(state.float())
        past_reward = self.dqn[1].policy_net(state.float().to('cuda'))

        return past_reward

    ''' Save an image of the current state of the network '''
    def render(self, i=0):
        node_labels = {}
        for node in self.dynetwork._network.nodes:
            node_labels[node] = len(self.dynetwork._network.nodes[node]['sending_queue']) + len(
                self.dynetwork._network.nodes[node]['receiving_queue'])
        nx.draw(self.dynetwork._network, pos=self._positions,
                labels=node_labels, font_weight='bold')
        if self.print_edge_weights:
            edge_labels = nx.get_edge_attributes(
                self.dynetwork._network, 'edge_delay')
            nx.draw_networkx_edge_labels(
                self.dynetwork._network, pos=self._positions, edge_labels=edge_labels)
        script_dir = os.path.dirname(__file__)
        results_dir = os.path.join(script_dir, 'network_images/')
        if not os.path.isdir(results_dir):
            os.makedirs(results_dir)
        plt.axis('off')
        plt.figtext(0.1, 0.1, "total injections: " +
                    str(self.max_initializations + self.dynetwork._initializations))
        plt.savefig("network_images/dynet" + str(i) + ".png")
        plt.clf()
