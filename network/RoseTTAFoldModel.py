import torch
import torch.nn as nn
from Embeddings import MSA_emb, Extra_emb, Templ_emb, Recycling
from Track_module import IterativeSimulator
from AuxiliaryPredictor import DistanceNetwork, MaskedTokenNetwork, ExpResolvedNetwork, LDDTNetwork, PAENetwork, BinderNetwork, EpitopeNetwork
from util import INIT_CRDS
from torch import einsum

#from pytorch_memlab import LineProfiler, profile

class RoseTTAFoldModule(nn.Module):
    def __init__(self, n_extra_block=4, n_main_block=8, n_ref_block=4,\
                 d_msa=256, d_msa_full=64, d_pair=128, d_templ=64,
                 n_head_msa=8, n_head_pair=4, n_head_templ=4,
                 d_hidden=32, d_hidden_templ=64,
                 p_drop=0.15, for_ab=False,
                 SE3_param_full={'l0_in_features':32, 'l0_out_features':16, 'num_edge_features':32},
                 SE3_param_topk={'l0_in_features':32, 'l0_out_features':16, 'num_edge_features':32},
                 ):
        super(RoseTTAFoldModule, self).__init__()
        #
        self.for_ab = for_ab

        # Input Embeddings
        d_state = SE3_param_topk['l0_out_features']
        self.latent_emb = MSA_emb(d_msa=d_msa, d_pair=d_pair, d_state=d_state, p_drop=p_drop, for_ab=for_ab)
        self.full_emb = Extra_emb(d_msa=d_msa_full, d_init=25, p_drop=p_drop)
        self.templ_emb = Templ_emb(d_pair=d_pair, d_templ=d_templ, d_state=d_state,
                                   n_head=n_head_templ,
                                   d_hidden=d_hidden_templ, p_drop=0.25)
        # Update inputs with outputs from previous round
        self.recycle = Recycling(d_msa=d_msa, d_pair=d_pair, d_state=d_state)
        #
        self.simulator = IterativeSimulator(n_extra_block=n_extra_block,
                                            n_main_block=n_main_block,
                                            n_ref_block=n_ref_block,
                                            d_msa=d_msa, d_msa_full=d_msa_full,
                                            d_pair=d_pair, d_hidden=d_hidden,
                                            n_head_msa=n_head_msa,
                                            n_head_pair=n_head_pair,
                                            SE3_param_full=SE3_param_full,
                                            SE3_param_topk=SE3_param_topk,
                                            p_drop=p_drop)
        ##
        self.c6d_pred = DistanceNetwork(d_pair, p_drop=p_drop)
        self.aa_pred = MaskedTokenNetwork(d_msa, p_drop=p_drop)
        self.lddt_pred = LDDTNetwork(d_state)
       
        self.exp_pred = ExpResolvedNetwork(d_msa, d_state)
        self.pae_pred = PAENetwork(d_pair)
        self.bind_pred = BinderNetwork() #fd - expose n_hidden as variable?
        
        if self.for_ab:
            self.epitope_pred = EpitopeNetwork(d_msa, d_state)

    #@profile
    def forward(self, msa_latent=None, msa_full=None, seq=None, xyz=None, idx=None,
                t1d=None, t2d=None, xyz_t=None, alpha_t=None, mask_t=None, chain_idx=None, same_chain=None,
                msa_prev=None, pair_prev=None, state_prev=None, mask_recycle=None,
                epitope_info=None,
                return_raw=False, return_full=False,
                use_checkpoint=False, p2p_crop=-1, topk_crop=-1, nc_cycle=False, 
                symmids=None, symmsub=None, symmRs=None, symmmeta=None,
                striping=None, low_vram=False):
        if symmids is None:
            symmids = torch.tensor([[0]], device=xyz.device) # C1
        oligo = symmids.shape[0]

        B, N, L = msa_latent.shape[:3]
        dtype = msa_latent.dtype

        # Get embeddings
        if striping is None:
            msa_stripe = -1
        else:
            msa_stripe = striping['msa_emb']
        msa_latent, pair, state = self.latent_emb(msa_latent, seq, idx, chain_idx=chain_idx, epi_info=epitope_info)
        msa_latent, pair, state = (
          msa_latent.to(dtype), pair.to(dtype), state.to(dtype)
        )

        msa_full = self.full_emb(msa_full, seq, idx, oligo)
        msa_full = msa_full.to(dtype)

        # Do recycling
        if msa_prev == None:
            msa_prev = torch.zeros_like(msa_latent[:,0])
            pair_prev = torch.zeros_like(pair)
            state_prev = torch.zeros_like(state)
        else:
            msa_prev = msa_prev.to(msa_latent.device)
            pair_prev = pair_prev.to(pair.device)

        if striping is None:
            recycl_stripe = -1
        else:
            recycl_stripe = striping['recycl']
        msa_recycle, pair_recycle, state_recycle = self.recycle(
          seq, msa_prev, pair_prev, state_prev, xyz, recycl_stripe, mask_recycle)
        msa_recycle, pair_recycle = msa_recycle.to(dtype), pair_recycle.to(dtype)

        msa_latent[:,0] += msa_recycle
        pair += pair_recycle
        state += state_recycle

        # free unused
        msa_prev, pair_prev, state_prev = None, None, None
        msa_recycle, pair_recycle, state_recycle = None, None, None

        #
        # add template embedding
        pair, state = self.templ_emb(
          t1d, t2d, alpha_t, xyz_t, mask_t, pair, state, strides=striping,
          use_checkpoint=use_checkpoint, p2p_crop=p2p_crop, symmids=symmids
        )

        # free unused
        t1d, t2d, alpha_t, xyz_t, mask_t = None, None, None, None, None

        # Predict coordinates from given inputs
        msa, pair, R, T, alpha, state, symmsub = self.simulator(
            seq, msa_latent, msa_full, pair, xyz[:,:,:3], state, idx, 
            striping, symmids, symmsub, symmRs, symmmeta,
            use_checkpoint=use_checkpoint, p2p_crop=p2p_crop, topk_crop=topk_crop, 
            low_vram=low_vram, nc_cycle=nc_cycle
        )

        if return_raw:
            # get last structure
            xyz = einsum('blij,blaj->blai', R[-1], xyz-xyz[:,:,1].unsqueeze(-2)) + T[-1].unsqueeze(-2)
            return msa[:,0], pair, state, xyz, alpha[-1], None

        # predict masked amino acids
        logits_aa = self.aa_pred(msa)

        # predict distogram & orientograms
        logits = self.c6d_pred(pair)

        # predict PAE
        logits_pae = self.pae_pred(pair)

        # predict bind/no-bind
        p_bind = self.bind_pred(logits_pae,same_chain)

        # Predict LDDT
        lddt = self.lddt_pred(state)

        # predict experimentally resolved or not
        logits_exp = self.exp_pred(msa[:,0], state)

        if self.for_ab:
            logits_epitope = self.epitope_pred(msa[:,0], state)

        # get all intermediate bb structures
        xyz = einsum('rblij,blaj->rblai', R, xyz-xyz[:,:,1].unsqueeze(-2)) + T.unsqueeze(-2)

        return logits, logits_aa, logits_exp, logits_pae, p_bind, xyz, alpha, symmsub, lddt, msa[:,0], pair, state
