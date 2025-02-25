# -*- coding: utf-8 -*-
import tensorflow as tf
import sys


def positional_embedding(pos_seq, inv_freq, bsz=None):
    sinusoid_inp = tf.compat.v1.einsum('i,j->ij', pos_seq, inv_freq)
    pos_emb = tf.compat.v1.concat([tf.compat.v1.sin(sinusoid_inp), tf.compat.v1.cos(sinusoid_inp)], -1)
    if bsz is not None:
        return tf.compat.v1.tile(pos_emb[:, None, :], [1, bsz, 1])
    else:
        return pos_emb[:, None, :]


def positionwise_FF(inp, d_model, d_inner, dropout, kernel_initializer,
                    scope='ff', is_training=True):
    output = inp
    with tf.compat.v1.variable_scope(scope):
        output = tf.compat.v1.layers.dense(inp, d_inner, activation=tf.compat.v1.nn.relu,
                                 kernel_initializer=kernel_initializer,
                                 name='layer_1')
        output = tf.compat.v1.layers.dropout(output, dropout, training=is_training,
                                   name='drop_1')
        output = tf.compat.v1.layers.dense(output, d_model,
                                 kernel_initializer=kernel_initializer,
                                 name='layer_2')
        output = tf.compat.v1.layers.dropout(output, dropout, training=is_training,
                                   name='drop_2')
        # output = tf.compat.v1.contrib.layers.layer_norm(output + inp, begin_norm_axis=-1)
    return output


def rel_shift(x):
    x_size = tf.compat.v1.shape(x)

    x = tf.compat.v1.pad(x, [[0, 0], [1, 0], [0, 0], [0, 0]])
    x = tf.compat.v1.reshape(x, [x_size[1] + 1, x_size[0], x_size[2], x_size[3]])
    x = tf.compat.v1.slice(x, [1, 0, 0, 0], [-1, -1, -1, -1])
    x = tf.compat.v1.reshape(x, x_size)

    return x


def rel_multihead_attn(w, r, r_w_bias, r_r_bias, attn_mask, mems, d_model,
                       n_head, d_head, dropout, dropatt, is_training,
                       kernel_initializer, scope='rel_attn'):
    scale = 1 / (d_head ** 0.5)
    with tf.compat.v1.variable_scope(scope):
        qlen = tf.compat.v1.shape(w)[0]
        rlen = tf.compat.v1.shape(r)[0]
        bsz = tf.compat.v1.shape(w)[1]

        cat = tf.compat.v1.concat([mems, w],
                        0) if mems is not None and mems.shape.ndims > 1 else w

        # cat = tf.compat.v1.concat([mems],
        #                 0) if mems is not None and mems.shape.ndims > 1 else w

        w_heads = tf.compat.v1.layers.dense(cat, 3 * n_head * d_head, use_bias=False,
                                  kernel_initializer=kernel_initializer, name='qkv')
        r_head_k = tf.compat.v1.layers.dense(r, n_head * d_head, use_bias=False,
                                   kernel_initializer=kernel_initializer, name='r')

        w_head_q, w_head_k, w_head_v = tf.compat.v1.split(w_heads, 3, -1)
        w_head_q = w_head_q[-qlen:]

        klen = tf.compat.v1.shape(w_head_k)[0]

        w_head_q = tf.compat.v1.reshape(w_head_q, [qlen, bsz, n_head, d_head])
        w_head_k = tf.compat.v1.reshape(w_head_k, [klen, bsz, n_head, d_head])
        w_head_v = tf.compat.v1.reshape(w_head_v, [klen, bsz, n_head, d_head])

        r_head_k = tf.compat.v1.reshape(r_head_k, [rlen, n_head, d_head])

        rw_head_q = w_head_q + r_w_bias
        rr_head_q = w_head_q + r_r_bias

        AC = tf.compat.v1.einsum('ibnd,jbnd->ijbn', rw_head_q, w_head_k)
        BD = tf.compat.v1.einsum('ibnd,jnd->ijbn', rr_head_q, r_head_k)
        BD = rel_shift(BD)

        attn_score = (AC + BD) * scale
        attn_mask_t = attn_mask[:, :, None, None]
        attn_score = attn_score * (1 - attn_mask_t) - 1e30 * attn_mask_t

        attn_prob = tf.compat.v1.nn.softmax(attn_score, 1)
        attn_prob = tf.compat.v1.layers.dropout(attn_prob, dropatt, training=is_training)

        attn_vec = tf.compat.v1.einsum('ijbn,jbnd->ibnd', attn_prob, w_head_v)
        size_t = tf.compat.v1.shape(attn_vec)
        attn_vec = tf.compat.v1.reshape(attn_vec, [size_t[0], size_t[1], n_head * d_head])

        attn_out = tf.compat.v1.layers.dense(attn_vec, d_model, use_bias=False,
                                   kernel_initializer=kernel_initializer, name='o')
        attn_out = tf.compat.v1.layers.dropout(attn_out, dropout, training=is_training)

        output = tf.compat.v1.contrib.layers.layer_norm(attn_out + w, begin_norm_axis=-1)
    return output


def embedding_lookup(lookup_table, x, use_tpu=True):
    if use_tpu:
        n_token = tf.compat.v1.shape(lookup_table)[0]
        one_hot_idx = tf.compat.v1.one_hot(x, n_token)
        if one_hot_idx.shape.ndims == 2:
            return tf.compat.v1.einsum('nd,in->id', lookup_table, one_hot_idx)
        else:
            return tf.compat.v1.einsum('nd,ibn->ibd', lookup_table, one_hot_idx)
    else:
        return tf.compat.v1.nn.embedding_lookup(lookup_table, x)


def mask_adaptive_embedding_lookup(x, n_token, d_embed, d_proj, cutoffs, initializer,
                                   proj_initializer, div_val=1,
                                   proj_same_dim=True,
                                   scope='adaptive_embed', **kwargs):
    emb_scale = d_proj ** 0.5
    with tf.compat.v1.variable_scope(scope):
        if div_val == 1:
            lookup_table = tf.compat.v1.get_variable('lookup_table', [n_token, d_embed],
                                           initializer=initializer)
            y = embedding_lookup(lookup_table, x, use_tpu=False)
            if d_proj != d_embed:
                proj_W = tf.compat.v1.get_variable('proj_W', [d_embed, d_proj],
                                         initializer=proj_initializer)
                y = tf.compat.v1.einsum('ibe,ed->ibd', y, proj_W)
            else:
                proj_W = None
            ret_params = [lookup_table, proj_W]
        else:
            tables, projs = [], []
            cutoff_ends = [0] + cutoffs + [n_token]
            x_size = tf.compat.v1.shape(x)
            y = tf.compat.v1.zeros([x_size[0], x_size[1], d_proj])
            for i in range(len(cutoff_ends) - 1):
                with tf.compat.v1.variable_scope('cutoff_{}'.format(i)):
                    l_idx, r_idx = cutoff_ends[i], cutoff_ends[i + 1]
                    mask = (x >= l_idx) & (x < r_idx)
                    cur_x = tf.compat.v1.boolean_mask(x, mask) - l_idx
                    cur_d_embed = d_embed // (div_val ** i)
                    lookup_table = tf.compat.v1.get_variable('lookup_table',
                                                   [r_idx - l_idx, cur_d_embed],
                                                   initializer=initializer)
                    cur_y = embedding_lookup(lookup_table, cur_x, use_tpu=False)
                    if d_proj == cur_d_embed and not proj_same_dim:
                        proj_W = None
                    else:
                        proj_W = tf.compat.v1.get_variable('proj_W', [cur_d_embed, d_proj],
                                                 initializer=proj_initializer)
                        cur_y = tf.compat.v1.einsum('id,de->ie', cur_y, proj_W)
                    mask_idx = tf.compat.v1.to_int64(tf.compat.v1.where(mask))
                    y += tf.compat.v1.scatter_nd(mask_idx, cur_y, tf.compat.v1.to_int64(tf.compat.v1.shape(y)))
                    tables.append(lookup_table)
                    projs.append(proj_W)
            ret_params = [tables, projs]

    y *= emb_scale
    return y, ret_params


def mul_adaptive_embedding_lookup(x, n_token, d_embed, d_proj, cutoffs, initializer,
                                  proj_initializer, div_val=1, perms=None,
                                  proj_same_dim=True,
                                  scope='adaptive_embed'):
    """
    perms: If None, first compute W = W1 x W2 (projection for each bin),
        and then compute X x W (embedding lookup). If not None,
        use bin-based embedding lookup with max_bin_size defined by
        the shape of perms.
    """
    emb_scale = d_proj ** 0.5
    with tf.compat.v1.variable_scope(scope):
        if div_val == 1:
            lookup_table = tf.compat.v1.get_variable('lookup_table', [n_token, d_embed],
                                           initializer=initializer)
            y = embedding_lookup(lookup_table, x)
            if d_proj != d_embed:
                proj_W = tf.compat.v1.get_variable('proj_W', [d_embed, d_proj],
                                         initializer=proj_initializer)
                y = tf.compat.v1.einsum('ibe,ed->ibd', y, proj_W)
            else:
                proj_W = None
            ret_params = [lookup_table, proj_W]
        else:
            tables, projs = [], []
            cutoff_ends = [0] + cutoffs + [n_token]
            x_size = tf.compat.v1.shape(x)
            if perms is None:
                cat_lookup = []
            else:
                cat_lookup = tf.compat.v1.zeros([x_size[0], x_size[1], d_proj])
            for i in range(len(cutoff_ends) - 1):
                with tf.compat.v1.variable_scope('cutoff_{}'.format(i)):
                    l_idx, r_idx = cutoff_ends[i], cutoff_ends[i + 1]
                    cur_d_embed = d_embed // (div_val ** i)
                    lookup_table = tf.compat.v1.get_variable('lookup_table',
                                                   [r_idx - l_idx, cur_d_embed],
                                                   initializer=initializer)
                    if cur_d_embed == d_proj and not proj_same_dim:
                        proj_W = None
                    else:
                        proj_W = tf.compat.v1.get_variable('proj_W', [cur_d_embed, d_proj],
                                                 initializer=proj_initializer)
                    if perms is None:
                        cat_lookup.append(tf.compat.v1.einsum('ie,ed->id', lookup_table, proj_W))
                    else:
                        # speed up the computation of the first bin
                        # also save some meory
                        if i == 0:
                            cur_y = embedding_lookup(lookup_table, tf.compat.v1.minimum(x, r_idx - 1))
                            if proj_W is not None:
                                cur_y = tf.compat.v1.einsum('ibe,ed->ibd', cur_y, proj_W)
                            cur_y *= perms[i][:, :, None]
                            cat_lookup += cur_y
                        else:
                            cur_x = tf.compat.v1.einsum('ib,ibk->k', tf.compat.v1.to_float(x - l_idx), perms[i])
                            cur_x = tf.compat.v1.to_int32(cur_x)
                            cur_y = embedding_lookup(lookup_table, cur_x)
                            if proj_W is not None:
                                cur_y = tf.compat.v1.einsum('ke,ed->kd', cur_y, proj_W)
                            cat_lookup += tf.compat.v1.einsum('kd,ibk->ibd', cur_y, perms[i])
                    tables.append(lookup_table)
                    projs.append(proj_W)
            if perms is None:
                cat_lookup = tf.compat.v1.concat(cat_lookup, 0)
                y = embedding_lookup(cat_lookup, x)
            else:
                y = cat_lookup
            ret_params = [tables, projs]

    y *= emb_scale
    return y, ret_params


def mask_adaptive_logsoftmax(hidden, target, n_token, d_embed, d_proj, cutoffs,
                             params, tie_projs,
                             initializer=None, proj_initializer=None,
                             div_val=1, scope='adaptive_softmax',
                             proj_same_dim=True,
                             return_mean=True, **kwargs):
    def _logit(x, W, b, proj):
        y = x
        if proj is not None:
            y = tf.compat.v1.einsum('ibd,ed->ibe', y, proj)
        return tf.compat.v1.einsum('ibd,nd->ibn', y, W) + b

    params_W, params_projs = params[0], params[1]

    def _gather_logprob(logprob, target):
        lp_size = tf.compat.v1.shape(logprob)
        r = tf.compat.v1.range(lp_size[0])
        idx = tf.compat.v1.stack([r, target], 1)
        return tf.compat.v1.gather_nd(logprob, idx)

    with tf.compat.v1.variable_scope(scope):
        if len(cutoffs) == 0:
            softmax_b = tf.compat.v1.get_variable('bias', [n_token],
                                        initializer=tf.compat.v1.zeros_initializer())
            output = _logit(hidden, params_W, softmax_b, params_projs)
            nll = tf.compat.v1.nn.sparse_softmax_cross_entropy_with_logits(labels=target, logits=output)
        else:
            cutoff_ends = [0] + cutoffs + [n_token]
            nll = tf.compat.v1.zeros_like(target, dtype=tf.compat.v1.float32)
            for i in range(len(cutoff_ends) - 1):
                with tf.compat.v1.variable_scope('cutoff_{}'.format(i)):
                    l_idx, r_idx = cutoff_ends[i], cutoff_ends[i + 1]
                    mask = (target >= l_idx) & (target < r_idx)
                    mask_idx = tf.compat.v1.where(mask)
                    cur_target = tf.compat.v1.boolean_mask(target, mask) - l_idx
                    cur_d_embed = d_embed // (div_val ** i)

                    if div_val == 1:
                        cur_W = params_W[l_idx: r_idx]
                    else:
                        cur_W = params_W[i]
                    cur_b = tf.compat.v1.get_variable('b', [r_idx - l_idx],
                                            initializer=tf.compat.v1.zeros_initializer())
                    if tie_projs[i]:
                        if div_val == 1:
                            cur_proj = params_projs
                        else:
                            cur_proj = params_projs[i]
                    else:
                        if (div_val == 1 or not proj_same_dim) and d_proj == cur_d_embed:
                            cur_proj = None
                        else:
                            cur_proj = tf.compat.v1.get_variable('proj', [cur_d_embed, d_proj],
                                                       initializer=proj_initializer)
                    if i == 0:
                        cluster_W = tf.compat.v1.get_variable('cluster_W', [len(cutoffs), d_embed],
                                                    initializer=tf.compat.v1.zeros_initializer())
                        cluster_b = tf.compat.v1.get_variable('cluster_b', [len(cutoffs)],
                                                    initializer=tf.compat.v1.zeros_initializer())
                        cur_W = tf.compat.v1.concat([cur_W, cluster_W], 0)
                        cur_b = tf.compat.v1.concat([cur_b, cluster_b], 0)

                        head_logit = _logit(hidden, cur_W, cur_b, cur_proj)
                        head_logprob = tf.compat.v1.nn.log_softmax(head_logit)
                        cur_head_logprob = tf.compat.v1.boolean_mask(head_logprob, mask)
                        cur_logprob = _gather_logprob(cur_head_logprob, cur_target)
                    else:
                        cur_head_logprob = tf.compat.v1.boolean_mask(head_logprob, mask)
                        cur_hidden = tf.compat.v1.boolean_mask(hidden, mask)
                        tail_logit = tf.compat.v1.squeeze(_logit(
                            cur_hidden[None], cur_W, cur_b, cur_proj), 0)
                        tail_logprob = tf.compat.v1.nn.log_softmax(tail_logit)
                        cur_logprob = (cur_head_logprob[:, cutoff_ends[1] + i - 1] +
                                       _gather_logprob(tail_logprob, cur_target))
                    nll += tf.compat.v1.scatter_nd(mask_idx, -cur_logprob,
                                         tf.compat.v1.to_int64(tf.compat.v1.shape(nll)))
    if return_mean:
        nll = tf.compat.v1.reduce_mean(nll)
    return nll


def mul_adaptive_logsoftmax(hidden, target, n_token, d_embed, d_proj, cutoffs,
                            params, tie_projs,
                            initializer=None, proj_initializer=None,
                            div_val=1, perms=None, proj_same_dim=True,
                            scope='adaptive_softmax',
                            **kwargs):
    def _logit(x, W, b, proj):
        y = x
        if x.shape.ndims == 3:
            if proj is not None:
                y = tf.compat.v1.einsum('ibd,ed->ibe', y, proj)
            return tf.compat.v1.einsum('ibd,nd->ibn', y, W) + b
        else:
            if proj is not None:
                y = tf.compat.v1.einsum('id,ed->ie', y, proj)
            return tf.compat.v1.einsum('id,nd->in', y, W) + b

    params_W, params_projs = params[0], params[1]

    with tf.compat.v1.variable_scope(scope):
        if len(cutoffs) == 0:
            softmax_b = tf.compat.v1.get_variable('bias', [n_token],
                                        initializer=tf.compat.v1.zeros_initializer())
            output = _logit(hidden, params_W, softmax_b, params_projs)
            nll = tf.compat.v1.nn.sparse_softmax_cross_entropy_with_logits(labels=target,
                                                                 logits=output)
            nll = tf.compat.v1.reduce_mean(nll)
        else:
            total_loss, total_cnt = 0, 0
            cutoff_ends = [0] + cutoffs + [n_token]
            for i in range(len(cutoff_ends) - 1):
                with tf.compat.v1.variable_scope('cutoff_{}'.format(i)):
                    l_idx, r_idx = cutoff_ends[i], cutoff_ends[i + 1]

                    cur_d_embed = d_embed // (div_val ** i)

                    if div_val == 1:
                        cur_W = params_W[l_idx: r_idx]
                    else:
                        cur_W = params_W[i]
                    cur_b = tf.compat.v1.get_variable('b', [r_idx - l_idx],
                                            initializer=tf.compat.v1.zeros_initializer())
                    if tie_projs[i]:
                        if div_val == 1:
                            cur_proj = params_projs
                        else:
                            cur_proj = params_projs[i]
                    else:
                        if (div_val == 1 or not proj_same_dim) and d_proj == cur_d_embed:
                            cur_proj = None
                        else:
                            cur_proj = tf.compat.v1.get_variable('proj', [cur_d_embed, d_proj],
                                                       initializer=proj_initializer)

                    if i == 0:
                        cluster_W = tf.compat.v1.get_variable('cluster_W', [len(cutoffs), d_embed],
                                                    initializer=tf.compat.v1.zeros_initializer())
                        cluster_b = tf.compat.v1.get_variable('cluster_b', [len(cutoffs)],
                                                    initializer=tf.compat.v1.zeros_initializer())
                        cur_W = tf.compat.v1.concat([cur_W, cluster_W], 0)
                        cur_b = tf.compat.v1.concat([cur_b, cluster_b], 0)

                        head_logit = _logit(hidden, cur_W, cur_b, cur_proj)

                        head_target = kwargs.get("head_target")
                        head_nll = tf.compat.v1.nn.sparse_softmax_cross_entropy_with_logits(
                            labels=head_target,
                            logits=head_logit)

                        masked_loss = head_nll * perms[i]
                        total_loss += tf.compat.v1.reduce_sum(masked_loss)
                        total_cnt += tf.compat.v1.reduce_sum(perms[i])

                        # head_logprob = tf.compat.v1.nn.log_softmax(head_logit)

                        # final_logprob = head_logprob * perms[i][:, :, None]
                        # final_target = tf.compat.v1.one_hot(target, tf.compat.v1.shape(head_logprob)[2])
                        # total_loss -= tf.compat.v1.einsum('ibn,ibn->', final_logprob, final_target)
                        # total_cnt += tf.compat.v1.reduce_sum(perms[i])
                    else:
                        cur_head_nll = tf.compat.v1.einsum('ib,ibk->k', head_nll, perms[i])

                        cur_hidden = tf.compat.v1.einsum('ibd,ibk->kd', hidden, perms[i])
                        tail_logit = _logit(cur_hidden, cur_W, cur_b, cur_proj)

                        tail_target = tf.compat.v1.einsum('ib,ibk->k', tf.compat.v1.to_float(target - l_idx),
                                                perms[i])
                        tail_nll = tf.compat.v1.nn.sparse_softmax_cross_entropy_with_logits(
                            labels=tf.compat.v1.to_int32(tail_target),
                            logits=tail_logit)

                        sum_nll = cur_head_nll + tail_nll
                        mask = tf.compat.v1.reduce_sum(perms[i], [0, 1])

                        masked_loss = sum_nll * mask
                        total_loss += tf.compat.v1.reduce_sum(masked_loss)
                        total_cnt += tf.compat.v1.reduce_sum(mask)

            nll = total_loss / total_cnt

    return nll


def _create_mask(qlen, mlen, same_length=False):
    attn_mask = tf.compat.v1.ones([qlen, qlen])
    mask_u = tf.compat.v1.matrix_band_part(attn_mask, 0, -1)
    mask_dia = tf.compat.v1.matrix_band_part(attn_mask, 0, 0)
    attn_mask_pad = tf.compat.v1.zeros([qlen, mlen])
    ret = tf.compat.v1.concat([attn_mask_pad, mask_u - mask_dia], 1)
    if same_length:
        mask_l = tf.compat.v1.matrix_band_part(attn_mask, -1, 0)
        ret = tf.compat.v1.concat([ret[:, :qlen] + mask_l - mask_dia, ret[:, qlen:]], 1)
    return ret


def _cache_mem(curr_out, prev_mem, mem_len=None):
    if mem_len is None or prev_mem is None:
        new_mem = curr_out
    elif mem_len == 0:
        return prev_mem
    else:
        new_mem = tf.compat.v1.concat([prev_mem, curr_out], 0)[- mem_len:]

    return tf.compat.v1.stop_gradient(new_mem)


def transformer(dec_inp, target, mems, n_token, n_layer, d_model, d_embed,
                n_head, d_head, d_inner, dropout, dropatt,
                initializer, is_training, proj_initializer=None,
                mem_len=None, cutoffs=[], div_val=1, tie_projs=[],
                same_length=False, clamp_len=-1, use_tpu=True,
                input_perms=None, target_perms=None, head_target=None,
                untie_r=False, proj_same_dim=True,
                scope='transformer'):
    """
    cutoffs: a list of python int. Cutoffs for adaptive softmax.
    tie_projs: a list of python bools. Whether to tie the projections.
    use_tpu: if True, use one_hot in embedding lookup and bin-based implementation
          of adaptive softmax.
    perms: a list of tensors. Each tensor should of size [len, bsz, bin_size].
          Only used in the adaptive setting.
    """
    # dec_inp = tf.compat.v1.Print(dec_inp, [dec_inp], "print of input: ")

    new_mems = []
    with tf.compat.v1.variable_scope(scope):
        if untie_r:
            r_w_bias = tf.compat.v1.get_variable('r_w_bias', [n_layer, n_head, d_head],
                                       initializer=initializer)
            r_r_bias = tf.compat.v1.get_variable('r_r_bias', [n_layer, n_head, d_head],
                                       initializer=initializer)
        else:
            r_w_bias = tf.compat.v1.get_variable('r_w_bias', [n_head, d_head],
                                       initializer=initializer)
            r_r_bias = tf.compat.v1.get_variable('r_r_bias', [n_head, d_head],
                                       initializer=initializer)

        qlen = tf.compat.v1.shape(dec_inp)[0]
        mlen = tf.compat.v1.shape(mems[0])[0] if mems is not None else 0
        klen = mlen + qlen

        if proj_initializer is None:
            proj_initializer = initializer
        lookup_fn = (mul_adaptive_embedding_lookup if use_tpu else
                     mask_adaptive_embedding_lookup)
        # 得到不同字的 enbedding
        embeddings, shared_params = lookup_fn(
            x=dec_inp,
            n_token=n_token,
            d_embed=d_embed,
            d_proj=d_model,
            cutoffs=cutoffs,
            initializer=initializer,
            proj_initializer=proj_initializer,
            div_val=div_val,
            perms=input_perms,
            proj_same_dim=proj_same_dim)

        # todo 这个mask 有什么用?????????
        attn_mask = _create_mask(qlen, mlen, same_length)

        pos_seq = tf.compat.v1.range(klen - 1, -1, -1.0)
        if clamp_len > 0:
            pos_seq = tf.compat.v1.minimum(pos_seq, clamp_len)
        inv_freq = 1 / (10000 ** (tf.compat.v1.range(0, d_model, 2.0) / d_model))
        pos_emb = positional_embedding(pos_seq, inv_freq)

        output = tf.compat.v1.layers.dropout(embeddings, dropout, training=is_training)
        pos_emb = tf.compat.v1.layers.dropout(pos_emb, dropout, training=is_training)

        if mems is None:
            mems = [None] * n_layer

        for i in range(n_layer):
            # cache new mems
            new_mems.append(_cache_mem(output, mems[i], mem_len))

            with tf.compat.v1.variable_scope('layer_{}'.format(i)):
                output = rel_multihead_attn(
                    w=output,
                    r=pos_emb,
                    r_w_bias=r_w_bias if not untie_r else r_w_bias[i],
                    r_r_bias=r_r_bias if not untie_r else r_r_bias[i],
                    attn_mask=attn_mask,
                    mems=mems[i],
                    d_model=d_model,
                    n_head=n_head,
                    d_head=d_head,
                    dropout=dropout,
                    dropatt=dropatt,
                    is_training=is_training,
                    kernel_initializer=initializer)
                output = positionwise_FF(
                    inp=output,
                    d_model=d_model,
                    d_inner=d_inner,
                    dropout=dropout,
                    kernel_initializer=initializer,
                    is_training=is_training)

        output = tf.compat.v1.layers.dropout(output, dropout, training=is_training)

        logsoftmax_fn = (mul_adaptive_logsoftmax if use_tpu else
                         mask_adaptive_logsoftmax)

        loss = logsoftmax_fn(
            hidden=output,
            target=target,
            n_token=n_token,
            d_embed=d_embed,
            d_proj=d_model,
            cutoffs=cutoffs,
            params=shared_params,
            tie_projs=tie_projs,
            initializer=initializer,
            proj_initializer=proj_initializer,
            div_val=div_val,
            perms=target_perms,
            head_target=head_target,
            proj_same_dim=proj_same_dim)
        return loss, new_mems


def transformer_inference(dec_inp, mems, mems_id, n_token, n_layer, d_model, d_embed,
                n_head, d_head, d_inner, dropout, dropatt,
                initializer, is_training, proj_initializer=None,
                mem_len=None, cutoffs=[], div_val=1, tie_projs=[],
                same_length=False, clamp_len=-1, use_tpu=True,
                input_perms=None, target_perms=None, head_target=None,
                untie_r=False, proj_same_dim=True,
                scope='transformer'):
    """
    cutoffs: a list of python int. Cutoffs for adaptive softmax.
    tie_projs: a list of python bools. Whether to tie the projections.
    use_tpu: if True, use one_hot in embedding lookup and bin-based implementation
          of adaptive softmax.
    perms: a list of tensors. Each tensor should of size [len, bsz, bin_size].
          Only used in the adaptive setting.
    """
    # dec_inp = tf.compat.v1.Print(dec_inp, [dec_inp], "print of input in inference : ")
    new_mems = []
    with tf.compat.v1.variable_scope(scope):
        if untie_r:
            r_w_bias = tf.compat.v1.get_variable('r_w_bias', [n_layer, n_head, d_head],
                                       initializer=initializer)
            r_r_bias = tf.compat.v1.get_variable('r_r_bias', [n_layer, n_head, d_head],
                                       initializer=initializer)
        else:
            r_w_bias = tf.compat.v1.get_variable('r_w_bias', [n_head, d_head],
                                       initializer=initializer)
            r_r_bias = tf.compat.v1.get_variable('r_r_bias', [n_head, d_head],
                                       initializer=initializer)

        qlen = tf.compat.v1.shape(dec_inp)[0]
        mlen = tf.compat.v1.shape(mems[0])[0] if mems is not None else 0
        klen = mlen + qlen

        if proj_initializer is None:
            proj_initializer = initializer
        lookup_fn = (mul_adaptive_embedding_lookup if use_tpu else
                     mask_adaptive_embedding_lookup)
        embeddings, shared_params = lookup_fn(
            x=dec_inp,
            n_token=n_token,
            d_embed=d_embed,
            d_proj=d_model,
            cutoffs=cutoffs,
            initializer=initializer,
            proj_initializer=proj_initializer,
            div_val=div_val,
            perms=input_perms,
            proj_same_dim=proj_same_dim)

        attn_mask = _create_mask(qlen, mlen, same_length)

        pos_seq = tf.compat.v1.range(klen - 1, -1, -1.0)
        if clamp_len > 0:
            pos_seq = tf.compat.v1.minimum(pos_seq, clamp_len)
        inv_freq = 1 / (10000 ** (tf.compat.v1.range(0, d_model, 2.0) / d_model))
        pos_emb = positional_embedding(pos_seq, inv_freq)

        output = tf.compat.v1.layers.dropout(embeddings, dropout, training=is_training)
        pos_emb = tf.compat.v1.layers.dropout(pos_emb, dropout, training=is_training)

        if mems is None:
            mems = [None] * n_layer

        if mems_id is None:
            mems_id = [None] * n_layer

        new_mem_id = []
        all_attn_prob = []
        for i in range(n_layer):
            # cache new mems
            new_mems.append(_cache_mem(output, mems[i], mem_len))
            new_mem_id.append(_cache_mem(dec_inp, mems_id[i], mem_len))

            with tf.compat.v1.variable_scope('layer_{}'.format(i)):
                output, attn_prob = rel_multihead_attn_for_inference(
                    w=output,
                    r=pos_emb,
                    r_w_bias=r_w_bias if not untie_r else r_w_bias[i],
                    r_r_bias=r_r_bias if not untie_r else r_r_bias[i],
                    attn_mask=attn_mask,
                    mems=mems[i],
                    d_model=d_model,
                    n_head=n_head,
                    d_head=d_head,
                    dropout=dropout,
                    dropatt=dropatt,
                    is_training=is_training,
                    kernel_initializer=initializer)
                all_attn_prob.append(attn_prob)

                output = positionwise_FF(
                    inp=output,
                    d_model=d_model,
                    d_inner=d_inner,
                    dropout=dropout,
                    kernel_initializer=initializer,
                    is_training=is_training)

        output = tf.compat.v1.layers.dropout(output, dropout, training=is_training)
        idx_output = compute_output(output, n_token, cutoffs, shared_params)

        loss = 0

        return new_mems, idx_output, new_mem_id, all_attn_prob


def compute_output(hidden, n_token, cutoffs, params, scope='adaptive_softmax'):
    def _logit(x, W, b, proj):
        y = x
        if proj is not None:
            y = tf.compat.v1.einsum('ibd,ed->ibe', y, proj)
        return tf.compat.v1.einsum('ibd,nd->ibn', y, W) + b

    params_W, params_projs = params[0], params[1]

    with tf.compat.v1.variable_scope(scope):
        if len(cutoffs) == 0:
            softmax_b = tf.compat.v1.get_variable('bias', [n_token],
                                        initializer=tf.compat.v1.zeros_initializer())
            output_idx = _logit(hidden, params_W, softmax_b, params_projs)
            return output_idx


def _cache_mem_id(curr_input, prev_mem, mem_len=None):
    if mem_len is None or prev_mem is None:
        new_mem = curr_input
    elif mem_len == 0:
        return prev_mem
    else:
        new_mem = tf.compat.v1.concat([prev_mem, curr_input], 0)[- mem_len:]

    return new_mem


def rel_multihead_attn_for_inference(w, r, r_w_bias, r_r_bias, attn_mask, mems, d_model,
                       n_head, d_head, dropout, dropatt, is_training,
                       kernel_initializer, scope='rel_attn'):
    scale = 1 / (d_head ** 0.5)
    with tf.compat.v1.variable_scope(scope):
        qlen = tf.compat.v1.shape(w)[0]
        rlen = tf.compat.v1.shape(r)[0]
        bsz = tf.compat.v1.shape(w)[1]

        cat = tf.compat.v1.concat([mems, w],
                        0) if mems is not None and mems.shape.ndims > 1 else w
        w_heads = tf.compat.v1.layers.dense(cat, 3 * n_head * d_head, use_bias=False,
                                  kernel_initializer=kernel_initializer, name='qkv')
        r_head_k = tf.compat.v1.layers.dense(r, n_head * d_head, use_bias=False,
                                   kernel_initializer=kernel_initializer, name='r')

        w_head_q, w_head_k, w_head_v = tf.compat.v1.split(w_heads, 3, -1)
        w_head_q = w_head_q[-qlen:]

        klen = tf.compat.v1.shape(w_head_k)[0]

        w_head_q = tf.compat.v1.reshape(w_head_q, [qlen, bsz, n_head, d_head])
        w_head_k = tf.compat.v1.reshape(w_head_k, [klen, bsz, n_head, d_head])
        w_head_v = tf.compat.v1.reshape(w_head_v, [klen, bsz, n_head, d_head])

        r_head_k = tf.compat.v1.reshape(r_head_k, [rlen, n_head, d_head])

        rw_head_q = w_head_q + r_w_bias
        rr_head_q = w_head_q + r_r_bias

        AC = tf.compat.v1.einsum('ibnd,jbnd->ijbn', rw_head_q, w_head_k)
        BD = tf.compat.v1.einsum('ibnd,jnd->ijbn', rr_head_q, r_head_k)
        BD = rel_shift(BD)

        attn_score = (AC + BD) * scale
        attn_mask_t = attn_mask[:, :, None, None]
        attn_score = attn_score * (1 - attn_mask_t) - 1e30 * attn_mask_t

        attn_prob = tf.compat.v1.nn.softmax(attn_score, 1)
        attn_prob = tf.compat.v1.layers.dropout(attn_prob, dropatt, training=is_training)

        attn_vec = tf.compat.v1.einsum('ijbn,jbnd->ibnd', attn_prob, w_head_v)
        size_t = tf.compat.v1.shape(attn_vec)
        attn_vec = tf.compat.v1.reshape(attn_vec, [size_t[0], size_t[1], n_head * d_head])

        attn_out = tf.compat.v1.layers.dense(attn_vec, d_model, use_bias=False,
                                   kernel_initializer=kernel_initializer, name='o')
        attn_out = tf.compat.v1.layers.dropout(attn_out, dropout, training=is_training)

        output = None
    return output, attn_prob
