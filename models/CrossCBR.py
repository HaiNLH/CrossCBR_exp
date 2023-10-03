#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch_sparse
from torch_sparse import SparseTensor
from torch_sparse.mul import mul
from torch.nn.parameter import Parameter
import scipy.sparse as sp 


def cal_bpr_loss(pred):
    # pred: [bs, 1+neg_num]
    if pred.shape[1] > 2:
        negs = pred[:, 1:]
        pos = pred[:, 0].unsqueeze(1).expand_as(negs)
    else:
        negs = pred[:, 1].unsqueeze(1)
        pos = pred[:, 0].unsqueeze(1)

    loss = - torch.log(torch.sigmoid(pos - negs)) # [bs]
    loss = torch.mean(loss)

    return loss


def laplace_transform(graph):
    rowsum_sqrt = sp.diags(1/(np.sqrt(graph.sum(axis=1).A.ravel()) + 1e-8))
    colsum_sqrt = sp.diags(1/(np.sqrt(graph.sum(axis=0).A.ravel()) + 1e-8))
    graph = rowsum_sqrt @ graph @ colsum_sqrt

    return graph


def to_tensor(graph):
    graph = graph.tocoo()
    values = graph.data
    indices = np.vstack((graph.row, graph.col))
    graph = torch.sparse.FloatTensor(torch.LongTensor(indices), torch.FloatTensor(values), torch.Size(graph.shape))

    return graph


def np_edge_dropout(values, dropout_ratio):
    mask = np.random.choice([0, 1], size=(len(values),), p=[dropout_ratio, 1-dropout_ratio])
    values = mask * values
    return values

class Info(object):
    '''
    [FOR `utils.logger`]

    the base class that packing all hyperparameters and infos used in the related model
    '''

    def __init__(self, embedding_size, embed_L2_norm):
        assert isinstance(embedding_size, int) and embedding_size > 0
        self.embedding_size = embedding_size
        assert embed_L2_norm >= 0
        self.embed_L2_norm = embed_L2_norm

    def get_title(self):
        dct = self.__dict__
        if '_info' in dct:
            dct.pop('_info')
        return '\t'.join(map(lambda x: dct[x].get_title() if isinstance(dct[x], Info) else x, dct.keys()))

    def get_csv_title(self):
        return self.get_title().replace('\t', ', ')

    def __getitem__(self, key):
        if hasattr(self, '_info'):
            return self._info[key]
        else:
            return self.__getattribute__(key)

    def __str__(self):
        dct = self.__dict__
        if '_info' in dct:
            dct.pop('_info')
        return '\t'.join(map(str, dct.values()))

    def get_line(self):
        return self.__str__()

    def get_csv_line(self):
        return self.get_line().replace('\t', ', ')
# class CrossCBR_Info(Info):
#     def __init__(self, embedding_size, embed_L2_norm, mess_dropout, node_dropout, num_layers, act=nn.LeakyReLU()):
#         super().__init__(embedding_size, embed_L2_norm)
#         self.act = act
#         assert 1 > mess_dropout >= 0
#         self.mess_dropout = mess_dropout
#         assert 1 > node_dropout >= 0
#         self.node_dropout = node_dropout
#         assert isinstance(num_layers, int) and num_layers > 0
#         self.num_layers = num_layers

class CrossCBR(nn.Module):
    def get_infotype(self):
        return CrossCBR_Info
    def __init__(self, conf, raw_graph):
        super().__init__()
        self.conf = conf
        device = self.conf["device"]
        self.device = device

        self.embedding_size = conf["embedding_size"]
        self.embed_L2_norm = conf["l2_reg"]
        self.num_users = conf["num_users"]
        self.num_bundles = conf["num_bundles"]
        self.num_items = conf["num_items"]
        self.n_factors = 4
        self.n_layers = 3
        self.num_layers = 2
        self.n_iterations = 2

        emb_dim = int(int(self.embedding_size) / self.n_factors)
        self.init_emb(emb_dim)
        
        # self.items_feature_each = nn.Parameter(
        #     torch.FloatTensor(self.num_items, emb_dim)).to(device)
        # nn.init.xavier_normal_(self.items_feature_each)
        # self.items_feature = torch.cat([self.items_feature_each for i in range(self.n_factors)], dim=1).to(device)

        assert isinstance(raw_graph, list)
        ub_graph, ui_graph, bi_graph = raw_graph
        self.ub_graph, self.ui_graph, self.bi_graph = raw_graph
        
        ### From MIDGN
        
        ui_graph_coo, ub_graph_coo, bi_graph_coo = ui_graph.tocoo(), ub_graph.tocoo(),bi_graph.tocoo()
        ub_indices = torch.tensor([ub_graph_coo.row, ub_graph_coo.col], dtype=torch.long)
        ub_values = torch.ones(ub_graph_coo.data.shape, dtype=torch.float)
        bi_indices = torch.tensor([bi_graph_coo.row, bi_graph_coo.col], dtype=torch.long)
        bi_values = torch.ones(bi_graph_coo.data.shape, dtype=torch.float)
        ui_e_indices, ui_e_values = torch_sparse.spspmm(ub_indices, ub_values, bi_indices, bi_values, self.num_users,
                                                        self.num_bundles, self.num_items)

        ui_graph_e = sp.csr_matrix((np.array([1] * len(ui_e_values)), (ui_e_indices[0].numpy(), ui_e_indices[1].numpy())),
            shape=(self.num_users, self.num_items))
        ui_graph_e_coo = ui_graph_e.tocoo()
        ui_graph_e_coo = ui_graph_e.tocoo()
        self.ui_mask = ui_graph_e[ui_graph_coo.row, ui_graph_coo.col]
        self.ui_e_mask = ui_graph[ui_graph_e_coo.row, ui_graph_e_coo.col]
        self.bi_graph, self.ui_graph = bi_graph, ui_graph
        ub_sparse = torch.sparse_coo_tensor(ub_indices, ub_values, ub_graph.shape)
        bi_sparse = torch.sparse_coo_tensor(bi_indices, bi_values, bi_graph.shape)

        # user-item graph (items from bundles users had interacted with)
        ubi_graph = self.get_ubi_non_weighted(ub_sparse, bi_sparse)
        # item-item graph each cell is the number of times item i and j appeared at a same bundle 
        self.ii_graph = bi_sparse.T @ bi_sparse 

        if ui_graph.shape == (self.num_users, self.num_items):
            # add self-loop
            atom_graph = sp.bmat([[sp.identity(ui_graph.shape[0]), ui_graph],
                                  [ui_graph.T, sp.identity(ui_graph.shape[1])]])
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        self.ui_atom_graph = to_tensor(laplace_transform(atom_graph)).to(device)
        if bi_graph.shape == (self.num_bundles, self.num_items):
            # add self-loop
            atom_graph = sp.bmat([[sp.identity(bi_graph.shape[0]), bi_graph],
                                  [bi_graph.T, sp.identity(bi_graph.shape[1])]])
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        self.bi_atom_graph = to_tensor(laplace_transform(atom_graph)).to(device)
        self.dnns_atom = nn.ModuleList([nn.Linear(
            self.embedding_size, self.embedding_size) for l in range(self.num_layers)])
        
        if bi_graph.shape == (self.num_bundles, self.num_items):
            tmp = bi_graph.tocoo()
            self.bi_graph_h = list(tmp.row)
            self.bi_graph_t = list(tmp.col)
            self.bi_graph_shape = bi_graph.shape
        else:
            raise ValueError(r"raw_graph's shape is wrong")

        if ui_graph.shape == (self.num_users, self.num_items):
            # add self-loop
            tmp = ui_graph.tocoo()
            self.ui_graph_v = torch.tensor(tmp.data, dtype=torch.float).to(device)
            self.ui_graph_h = list(tmp.row)
            self.ui_graph_t = list(tmp.col)
            self.ui_graph_shape = ui_graph.shape
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        
        if ub_graph.shape == (self.num_users, self.num_bundles):
            # add self-loop
            tmp = ub_graph.tocoo()
            self.ub_graph_v = torch.tensor(tmp.data, dtype=torch.float).to(device)
            self.ub_graph_h = list(tmp.row)
            self.ub_graph_t = list(tmp.col)
            self.ub_graph_shape = ub_graph.shape
        else:
            raise ValueError(r"raw_graph's shape is wrong")

        #  deal with weights
        bi_norm = sp.diags(1 / (np.sqrt((bi_graph.multiply(bi_graph)).sum(axis=1).A.ravel()) + 1e-8)) @ bi_graph
        bb_graph = bi_norm @ bi_norm.T

        bundle_size = bi_graph.sum(axis=1) + 1e-8
        bi_graph = sp.diags(1 / bundle_size.A.ravel()) @ bi_graph

        if ub_graph.shape == (self.num_users, self.num_bundles) \
                and bb_graph.shape == (self.num_bundles, self.num_bundles):
            # add self-loop
            non_atom_graph = sp.bmat([[sp.identity(ub_graph.shape[0]), ub_graph],
                                      [ub_graph.T, bb_graph]])
        else:
            raise ValueError(r"raw_graph's shape is wrong")
        self.non_atom_graph = to_tensor(laplace_transform(non_atom_graph)).to(device)
        print('finish generating non-atom graph')
        # self.act = self.info.act
        # self.num_layers = self.info.num_layers
        # self.device = device

        #  Dropouts
        # self.mess_dropout = nn.Dropout(self.info.mess_dropout, True)
        # self.node_dropout = nn.Dropout(self.info.node_dropout, True)

        # Layers
        self.dnns_non_atom = nn.ModuleList([nn.Linear(
            self.embedding_size, self.embedding_size) for l in range(self.num_layers)])
        
        ### MIDGN
        # generate the graph without any dropouts for testing
        self.get_item_level_graph_ori()
        self.get_bundle_level_graph_ori()
        self.get_bundle_agg_graph_ori()

        # generate the graph with the configured dropouts for training, if aug_type is OP or MD, the following graphs with be identical with the aboves
        self.get_item_level_graph()
        self.get_bundle_level_graph()
        self.get_bundle_agg_graph()

        self.init_md_dropouts()

        self.num_layers = self.conf["num_layers"]
        self.c_temp = self.conf["c_temp"]


    def init_md_dropouts(self):
        self.item_level_dropout = nn.Dropout(self.conf["item_level_ratio"], True)
        self.bundle_level_dropout = nn.Dropout(self.conf["bundle_level_ratio"], True)
        self.bundle_agg_dropout = nn.Dropout(self.conf["bundle_agg_ratio"], True)


    def init_emb(self, embed_dim):
        self.users_feature = nn.Parameter(torch.FloatTensor(self.num_users, self.embedding_size))
        nn.init.xavier_normal_(self.users_feature)
        self.bundles_feature = nn.Parameter(torch.FloatTensor(self.num_bundles, self.embedding_size))
        nn.init.xavier_normal_(self.bundles_feature)
        self.items_feature_each = nn.Parameter(
            torch.FloatTensor(self.num_items, embed_dim))
        # self.items_feature = nn.Parameter(torch.FloatTensor(self.num_items, self.embedding_size))
        self.items_feature = torch.cat([self.items_feature_each for i in range(self.n_factors)], dim=1)
        nn.init.xavier_normal_(self.items_feature)


    def get_item_level_graph(self):
        ui_graph = self.ui_graph
        device = self.device
        modification_ratio = self.conf["item_level_ratio"]

        item_level_graph = sp.bmat([[sp.csr_matrix((ui_graph.shape[0], ui_graph.shape[0])), ui_graph], [ui_graph.T, sp.csr_matrix((ui_graph.shape[1], ui_graph.shape[1]))]])
        if modification_ratio != 0:
            if self.conf["aug_type"] == "ED":
                graph = item_level_graph.tocoo()
                values = np_edge_dropout(graph.data, modification_ratio)
                item_level_graph = sp.coo_matrix((values, (graph.row, graph.col)), shape=graph.shape).tocsr()

        self.item_level_graph = to_tensor(laplace_transform(item_level_graph)).to(device)


    def get_item_level_graph_ori(self):
        ui_graph = self.ui_graph
        device = self.device
        item_level_graph = sp.bmat([[sp.csr_matrix((ui_graph.shape[0], ui_graph.shape[0])), ui_graph], [ui_graph.T, sp.csr_matrix((ui_graph.shape[1], ui_graph.shape[1]))]])
        self.item_level_graph_ori = to_tensor(laplace_transform(item_level_graph)).to(device)


    def get_bundle_level_graph(self):
        ub_graph = self.ub_graph
        device = self.device
        modification_ratio = self.conf["bundle_level_ratio"]

        bundle_level_graph = sp.bmat([[sp.csr_matrix((ub_graph.shape[0], ub_graph.shape[0])), ub_graph], [ub_graph.T, sp.csr_matrix((ub_graph.shape[1], ub_graph.shape[1]))]])

        if modification_ratio != 0:
            if self.conf["aug_type"] == "ED":
                graph = bundle_level_graph.tocoo()
                values = np_edge_dropout(graph.data, modification_ratio)
                bundle_level_graph = sp.coo_matrix((values, (graph.row, graph.col)), shape=graph.shape).tocsr()

        self.bundle_level_graph = to_tensor(laplace_transform(bundle_level_graph)).to(device)


    def get_bundle_level_graph_ori(self):
        ub_graph = self.ub_graph
        device = self.device
        bundle_level_graph = sp.bmat([[sp.csr_matrix((ub_graph.shape[0], ub_graph.shape[0])), ub_graph], [ub_graph.T, sp.csr_matrix((ub_graph.shape[1], ub_graph.shape[1]))]])
        self.bundle_level_graph_ori = to_tensor(laplace_transform(bundle_level_graph)).to(device)


    def get_bundle_agg_graph(self):
        bi_graph = self.bi_graph
        device = self.device

        if self.conf["aug_type"] == "ED":
            modification_ratio = self.conf["bundle_agg_ratio"]
            graph = self.bi_graph.tocoo()
            values = np_edge_dropout(graph.data, modification_ratio)
            bi_graph = sp.coo_matrix((values, (graph.row, graph.col)), shape=graph.shape).tocsr()

        bundle_size = bi_graph.sum(axis=1) + 1e-8
        bi_graph = sp.diags(1/bundle_size.A.ravel()) @ bi_graph
        self.bundle_agg_graph = to_tensor(bi_graph).to(device)


    def get_bundle_agg_graph_ori(self):
        bi_graph = self.bi_graph
        device = self.device

        bundle_size = bi_graph.sum(axis=1) + 1e-8
        bi_graph = sp.diags(1/bundle_size.A.ravel()) @ bi_graph
        self.bundle_agg_graph_ori = to_tensor(bi_graph).to(device)


    def one_propagate(self, graph, A_feature, B_feature, mess_dropout, test):
        features = torch.cat((A_feature, B_feature), 0)
        all_features = [features]

        for i in range(self.num_layers):
            features = torch.spmm(graph, features)
            if self.conf["aug_type"] == "MD" and not test: # !!! important
                features = mess_dropout(features)

            features = features / (i+2)
            all_features.append(F.normalize(features, p=2, dim=1))

        all_features = torch.stack(all_features, 1)
        all_features = torch.sum(all_features, dim=1).squeeze(1)

        A_feature, B_feature = torch.split(all_features, (A_feature.shape[0], B_feature.shape[0]), 0)

        return A_feature, B_feature


    def get_IL_bundle_rep(self, IL_items_feature, test):
        if test:
            IL_bundles_feature = torch.matmul(self.bundle_agg_graph_ori, IL_items_feature)
        else:
            IL_bundles_feature = torch.matmul(self.bundle_agg_graph, IL_items_feature)

        # simple embedding dropout on bundle embeddings
        if self.conf["bundle_agg_ratio"] != 0 and self.conf["aug_type"] == "MD" and not test:
            IL_bundles_feature = self.bundle_agg_dropout(IL_bundles_feature)

        return IL_bundles_feature
    # def ub_propagate(self, graph, A_feature, B_feature):
    #     # node dropout on graph
    #     # indices = graph._indices()
    #     # values = graph._values()
    #     # values = self.node_dropout(values)
    #     # graph = torch.sparse.FloatTensor(
    #     #     indices, values, size=graph.shape)

    #     # propagate
    #     features = torch.cat((A_feature, B_feature), 0)

    #     all_features = torch.matmul(graph, features)
    #     # all_features=torch.mean(all_features,dim=1,keepdims=False)
    #     A_feature, B_feature = torch.split(
    #         all_features, (A_feature.shape[0], B_feature.shape[0]), 0)
    #     return A_feature, B_feature

    def propagate(self, test=False):  #Cần xem lại logic đoạn này vì trong MIDGN chia U-I và B-I và 2 phần này đều là item view trong Cross CBR - trong CrossCBR có thêm phần bundle view U-B
        #  =============================  item level propagation  =============================
        if test:
            IL_users_feature, IL_items_feature = self.one_propagate(self.item_level_graph_ori, self.users_feature, self.items_feature, self.item_level_dropout, test)
            TL_bundles_feature, TL_item_feature_bundle, self.bi_avalues = self._create_star_routing_embed_with_p(self.bi_graph_h,
                                                                                                     self.bi_graph_t,
                                                                                                     self.bundles_feature,
                                                                                                     self.items_feature,
                                                                                                     self.num_bundles,
                                                                                                     self.num_items,
                                                                                                     self.bi_graph_shape,
                                                                                                     n_factors=1,
                                                                                                     pick_=False)
            TL_user_feature, TL_item_feature_user, self.ui_avalues = self._create_star_routing_embed_with_p(self.ui_graph_h,
                                                                                                   self.ui_graph_t,
                                                                                                   self.users_feature,
                                                                                                   self.items_feature,
                                                                                                   self.num_users,
                                                                                                   self.num_items,
                                                                                                   self.ui_graph_shape,
                                                                                                   n_factors=self.n_factors,
                                                                                                   pick_=False)
        else:
            TL_bundles_feature, TL_item_feature_bundle, self.bi_avalues = self._create_star_routing_embed_with_p(self.bi_graph_h,
                                                                                                     self.bi_graph_t,
                                                                                                     self.bundles_feature,
                                                                                                     self.items_feature,
                                                                                                     self.num_bundles,
                                                                                                     self.num_items,
                                                                                                     self.bi_graph_shape,
                                                                                                     n_factors=1,
                                                                                                     pick_=False)
            IL_users_feature, IL_items_feature = self.one_propagate(self.item_level_graph, self.users_feature, self.items_feature, self.item_level_dropout, test)

        # aggregate the items embeddings within one bundle to obtain the bundle representation
        IL_bundles_feature = self.get_IL_bundle_rep(IL_items_feature, test)

        #  ============================= bundle level propagation =============================
        if test:
            BL_users_feature, BL_bundles_feature = self.one_propagate(self.bundle_level_graph_ori, self.users_feature, self.bundles_feature, self.bundle_level_dropout, test)
        else:
            TL_user_feature, TL_item_feature_user, self.ui_avalues = self._create_star_routing_embed_with_p(self.ui_graph_h,
                                                                                                   self.ui_graph_t,
                                                                                                   self.users_feature,
                                                                                                   self.items_feature,
                                                                                                   self.num_users,
                                                                                                   self.num_items,
                                                                                                   self.ui_graph_shape,
                                                                                                   n_factors=self.n_factors,
                                                                                                   pick_=False)
            BL_users_feature, BL_bundles_feature = self.one_propagate(self.bundle_level_graph, self.users_feature, self.bundles_feature, self.bundle_level_dropout, test)
        ui_avalues_e_list = []
        ui_avalues_list = []

        users_feature = [IL_users_feature, BL_users_feature, TL_user_feature]
        bundles_feature = [IL_bundles_feature, BL_bundles_feature, TL_bundles_feature]

        return users_feature, bundles_feature

    def get_ubi_non_weighted(self, ub, bi):
        '''
        ub : user-bunlde coo-graph
        bi : bundle-item coo-graph

        return: ubi user-item coo-graph through bundle 
        each cell [i,j] == 0 or 1
        '''
        temp = ub @ bi
        idx = temp.indices()
        val = torch.ones_like(temp.values())
        ubi = torch.sparse_coo_tensor(indices=idx, values=val, size=temp.shape)
        return ubi
    def cal_c_loss(self, pos, aug):
        # pos: [batch_size, :, emb_size]
        # aug: [batch_size, :, emb_size]
        pos = pos[:, 0, :]
        aug = aug[:, 0, :]

        pos = F.normalize(pos, p=2, dim=1)
        aug = F.normalize(aug, p=2, dim=1)
        pos_score = torch.sum(pos * aug, dim=1) # [batch_size]
        ttl_score = torch.matmul(pos, aug.permute(1, 0)) # [batch_size, batch_size]

        pos_score = torch.exp(pos_score / self.c_temp) # [batch_size]
        ttl_score = torch.sum(torch.exp(ttl_score / self.c_temp), axis=1) # [batch_size]

        c_loss = - torch.mean(torch.log(pos_score / ttl_score))

        return c_loss


    def cal_loss(self, users_feature, bundles_feature):
        # IL: item_level, BL: bundle_level, TL: Intent-level
        # [bs, 1, emb_size]
        IL_users_feature, BL_users_feature, TL_users_feature = users_feature
        # [bs, 1+neg_num, emb_size]
        IL_bundles_feature, BL_bundles_feature, TL_bundles_feature = bundles_feature
        # [bs, 1+neg_num]
        pred = torch.sum(IL_users_feature * IL_bundles_feature, 2) + torch.sum(BL_users_feature * BL_bundles_feature, 2) + torch.sum(TL_users_feature * TL_bundles_feature, 2)
        bpr_loss = cal_bpr_loss(pred)

        # cl is abbr. of "contrastive loss"
        u_cross_view_cl = self.cal_c_loss(IL_users_feature, BL_users_feature)
        b_cross_view_cl = self.cal_c_loss(IL_bundles_feature, BL_bundles_feature)

        c_losses = [u_cross_view_cl, b_cross_view_cl]

        c_loss = sum(c_losses) / len(c_losses)

        return bpr_loss, c_loss
    # /* MIDGN
    # ----------------------------------------------------------
    # *
    def _create_star_routing_embed_with_p(self, all_h_list, all_t_list, featureA, featureB, numA, numB, A_inshape, n_factors=4,
                                          pick_=False):
        '''
        pick_ : True, the model would narrow the weight of the least important factor down to 1/args.pick_scale.
        pick_ : False, do nothing.
        '''
        '''
        need parameter:
        n_factor

        user_embedding --> bundle_feature
        item_embedding --> item_feature
        self.A_in_shape
        A:all_h_list, all_t_list

        '''
        p_test = False
        p_train = False
        A_indices = torch.tensor([all_h_list, all_t_list], dtype=torch.long).to(self.device)
        D_indices_col = torch.tensor([list(range(numA)), list(range(numA))]).to(self.device)
        D_indices_row = torch.tensor([list(range(numB)), list(range(numB))]).to(self.device)
        A_values = torch.ones(n_factors, len(all_h_list)).to(self.device)
        
        all_A_embeddings = [featureA]
        all_B_embeddings = [featureB]
        factor_num = [n_factors, n_factors, n_factors, n_factors, n_factors, n_factors]
        iter_num = [self.n_iterations, self.n_iterations, self.n_iterations, self.n_iterations, self.n_iterations,
                    self.n_iterations]
        for k in range(0, self.n_layers):
            # prepare the output embedding list
            # .... layer_embeddings stores a (n_factors)-len list of outputs derived from the last routing iterations.
            n_factors_l = factor_num[k]
            n_iterations_l = iter_num[k]
            A_layer_embeddings = []
            B_layer_embeddings = []

            # split the input embedding table
            # .... ego_layer_embeddings is a (n_factors)-len list of embeddings [n_users+n_items, embed_size/n_factors]
            ego_layer_A_embeddings = torch.split(featureA, int(featureA.shape[1] / n_factors_l), 1)
            ego_layer_B_embeddings = torch.split(featureB, int(featureB.shape[1] / n_factors_l), 1)
            # ego_layer_embeddings=[torch.cat([A, featureB], 0) for A in ego_layer_A_embeddings]
            # perform routing mechanism
            for t in range(0, n_iterations_l):
                A_iter_embeddings = []
                B_iter_embeddings = []
                A_iter_values = []

                # split the adjacency values & get three lists of [n_users+n_items, n_users+n_items] sparse tensors
                # .... A_factors is a (n_factors)-len list, each of which is an adjacency matrix
                # .... D_col_factors is a (n_factors)-len list, each of which is a degree matrix w.r.t. columns
                # .... D_row_factors is a (n_factors)-len list, each of which is a degree matrix w.r.t. rows
                if t == n_iterations_l - 1:
                    p_test = pick_
                    p_train = False

                A_factors, A_factors_t, D_col_factors, D_row_factors = self._convert_A_values_to_A_factors_with_P(
                    n_factors_l,
                    A_values,
                    all_h_list,
                    all_t_list,
                    numA,
                    numB,
                    A_inshape,
                    pick=p_train)
                for i in range(0, n_factors_l):
                    
                    A_factor_embeddings = torch_sparse.spmm(D_indices_row.to(self.device), D_row_factors[i].to(self.device), A_inshape[1].to(self.device), A_inshape[1].to(self.device),
                                                            ego_layer_B_embeddings[i].to(self.device))
                    A_factor_embeddings = torch_sparse.spmm(A_indices.to(self.device), A_factors[i].to(self.device), A_inshape[0].to(self.device), A_inshape[1].to(self.device),
                                                            A_factor_embeddings.to(self.device))  # torch.sparse.mm(A_factors[i], factor_embeddings)

                    A_factor_embeddings = torch_sparse.spmm(D_indices_col.to(self.device), D_col_factors[i].to(self.device), A_inshape[0].to(self.device), A_inshape[0].to(self.device),
                                                            A_factor_embeddings.to(self.device))
                    A_iter_embedding = ego_layer_A_embeddings[i].to(self.device) + A_factor_embeddings.to(self.device)

                    B_factor_embeddings = torch_sparse.spmm(D_indices_col.to(self.device), D_col_factors[i].to(self.device), A_inshape[0].to(self.device), A_inshape[0].to(self.device),
                                                            ego_layer_A_embeddings[i].to(self.device))
                    B_factor_embeddings = torch_sparse.spmm(A_indices[[1, 0]].to(self.device), A_factors_t[i].to(self.device), A_inshape[1].to(self.device),
                                                            A_inshape[0].to(self.device),
                                                            B_factor_embeddings.to(self.device))  # torch.sparse.mm(A_factors[i], factor_embeddings)

                    B_factor_embeddings = torch_sparse.spmm(D_indices_row.to(self.device), D_row_factors[i].to(self.device), A_inshape[1].to(self.device), A_inshape[1].to(self.device),
                                                            B_factor_embeddings.to(self.device))
                    B_iter_embedding = ego_layer_B_embeddings[i].to(self.device) + B_factor_embeddings.to(self.device)
                    # A_iter_embedding,B_iter_embedding=torch.split(factor_embeddings, [numA, numB], 0)
                    A_iter_embeddings.append(A_iter_embedding)
                    B_iter_embeddings.append(B_iter_embedding)

                    if t == n_iterations_l - 1:
                        A_layer_embeddings = A_iter_embeddings
                        B_layer_embeddings = B_iter_embeddings
                        # get the factor-wise embeddings
                    # .... head_factor_embeddings is a dense tensor with the size of [all_h_list, embed_size/n_factors]
                    # .... analogous to tail_factor_embeddings
                    head_factor_embedings = A_iter_embedding[all_h_list]
                    tail_factor_embedings = ego_layer_B_embeddings[i][all_t_list]

                    # .... constrain the vector length
                    # .... make the following attentive weights within the range of (0,1)
                    head_factor_embedings = F.normalize(head_factor_embedings, dim=1).to(self.device)
                    tail_factor_embedings = F.normalize(tail_factor_embedings, dim=1).to(self.device)

                    # get the attentive weights
                    # .... A_factor_values is a dense tensor with the size of [all_h_list,1]
                    A_factor_values = torch.sum(torch.mul(head_factor_embedings, F.tanh(tail_factor_embedings)), axis=1).to(self.device)

                    # update the attentive weights
                    A_iter_values.append(A_factor_values)

                # pack (n_factors) adjacency values into one [n_factors, all_h_list] tensor
                A_iter_values = torch.stack(A_iter_values, 0)
                # add all layer-wise attentive weights up.
                A_values = A_values + A_iter_values

            # sum messages of neighbors, [n_users+n_items, embed_size]
            # side_embeddings = torch.cat(layer_embeddings, 1)

            # ego_embeddings = side_embeddings
            # concatenate outputs of all layers
            featureA = torch.cat(A_layer_embeddings, 1)
            featureB = torch.cat(B_layer_embeddings, 1)
            all_A_embeddings = all_A_embeddings + [featureA]
            all_B_embeddings = all_B_embeddings + [featureB]
        #all_A_embeddings = torch.cat(all_A_embeddings, 1)
        #all_B_embeddings = torch.cat(all_B_embeddings, 1)
        all_A_embeddings = torch.stack(all_A_embeddings, 1)
        all_A_embeddings = torch.mean(all_A_embeddings, dim=1, keepdims=False)
        all_B_embeddings = torch.stack(all_B_embeddings, 1)
        all_B_embeddings = torch.mean(all_B_embeddings, dim=1, keepdims=False)

        return all_A_embeddings.to(self.device), all_B_embeddings.to(self.device), A_values.to(self.device)

    def _convert_A_values_to_A_factors_with_P(self, f_num, A_factor_values, all_h_list, all_t_list, numA, numB,
                                              A_inshape, pick=False):
        A_factors = []
        A_factors_t = []
        D_col_factors = []
        D_row_factors = []
        all_h_list = torch.tensor(all_h_list, dtype=torch.long).to(self.device)
        all_t_list = torch.tensor(all_t_list, dtype=torch.long).to(self.device)
        # get the indices of adjacency matrix

        A_indices = torch.stack([all_h_list, all_t_list], dim=0)
        # print(A_indices.shape)
        # apply factor-aware softmax function over the values of adjacency matrix
        # ....A_factor_values is [n_factors, all_h_list]
        if pick:
            A_factor_scores = F.softmax(A_factor_values, 0)
            min_A = torch.min(A_factor_scores, 0)
            index = A_factor_scores > (min_A + 0.0000001)
            index = index.type(torch.float32) * (
                    self.pick_level - 1.0) + 1.0  # adjust the weight of the minimum factor to 1/self.pick_level

            A_factor_scores = A_factor_scores * index
            A_factor_scores = A_factor_scores / torch.sum(A_factor_scores, 0)
        else:
            A_factor_scores = F.softmax(A_factor_values, 0)

        for i in range(0, f_num):
            # in the i-th factor, couple the adjcency values with the adjacency indices
            # .... A i-tensor is a sparse tensor with size of [n_users+n_items,n_users+n_items]
            A_i_scores = A_factor_scores[i]
            # A_i_tensor = torch.sparse_coo_tensor(A_indices, A_i_scores, A_inshape).to(self.device)
            A_i_tensor = SparseTensor(row=all_h_list, col=all_t_list, value=A_i_scores,
                                      sparse_sizes=(A_inshape[0], A_inshape[1]))
            D_i_col_scores = 1 / (torch.sqrt(A_i_tensor.sum(dim=1)) + 1e-10)
            D_i_row_scores = 1 / (torch.sqrt(A_i_tensor.sum(dim=0)) + 1e-10)
            _, A_i_scores_t = torch_sparse.transpose(A_indices, A_i_scores, A_inshape[0], A_inshape[1])
            A_factors.append(A_i_scores.to(self.device))
            A_factors_t.append(A_i_scores_t.to(self.device))
            D_col_factors.append(D_i_col_scores)
            D_row_factors.append(D_i_row_scores)

        # return a (n_factors)-length list of laplacian matrix
        return A_factors, A_factors_t, D_col_factors, D_row_factors

    ###MIDGN
    ###----end----###

    def forward(self, batch, ED_drop=False):
        # the edge drop can be performed by every batch or epoch, should be controlled in the train loop
        if ED_drop:
            self.get_item_level_graph()
            self.get_bundle_level_graph()
            self.get_bundle_agg_graph()

        # users: [bs, 1]
        # bundles: [bs, 1+neg_num]
        users, bundles = batch
        users_feature, bundles_feature = self.propagate()

        users_embedding = [i[users].expand(-1, bundles.shape[1], -1) for i in users_feature]
        bundles_embedding = [i[bundles] for i in bundles_feature]

        bpr_loss, c_loss = self.cal_loss(users_embedding, bundles_embedding)

        return bpr_loss, c_loss


    def evaluate(self, propagate_result, users):
        users_feature, bundles_feature = propagate_result
        users_feature_atom, users_feature_non_atom = [i[users] for i in users_feature]
        bundles_feature_atom, bundles_feature_non_atom = bundles_feature

        scores = torch.mm(users_feature_atom, bundles_feature_atom.t()) + torch.mm(users_feature_non_atom, bundles_feature_non_atom.t())
        return scores
