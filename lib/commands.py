#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import datetime
import time
import copy
import argparse
import json
import ast
import base64
from functools import wraps
from decimal import Decimal
import logging

import util
from util import print_msg, format_satoshis, print_stderr, NotEnoughFunds
import lbrycrd
from lbrycrd import is_address, hash_160_to_bc_address, hash_160, COIN, TYPE_ADDRESS, Hash
from lbrycrd import TYPE_CLAIM, TYPE_SUPPORT, TYPE_UPDATE, RECOMMENDED_CLAIMTRIE_HASH_CONFIRMS
from transaction import Transaction
from transaction import deserialize as deserialize_transaction, script_GetOp, decode_claim_script
import paymentrequest
from paymentrequest import PR_PAID, PR_UNPAID, PR_UNKNOWN, PR_EXPIRED
import contacts
from claims import verify_proof, InvalidProofError


log = logging.getLogger(__name__)

known_commands = {}

# Format output from lbrycrd to have consistently
# named ditionary keys
def format_lbrycrd_keys(obj):
    if isinstance(obj, dict):
        for key, val in obj.iteritems():
            new_key = key
            if key == 'n' or key == 'nOut':
                new_key = 'nout'
            elif key == 'nAmount':
                new_key = 'amount'
            elif key == 'nEffectiveAmount':
                new_key = 'effective_amount'
            elif key == 'claimId':
                new_key = 'claim_id'
            elif key == 'nHeight':
                new_key = 'height'
            elif key == 'nValidAtHeight':
                new_key = 'valid_at_height'
            elif key == 'nLastTakeoverHeight':
                new_key = 'last_takeover_height'
            elif key == 'supports without claims':
                new_key = 'supports_without_claims'
            elif key == 'is controlling':
                new_key = 'is_controlling'
            elif key == 'in claim trie':
                new_key = 'in_claim_trie'
            if new_key != key:
                obj[new_key] = obj[key]
                del obj[key]

            if isinstance(val, list) or isinstance(val, dict):
                obj[new_key] = format_lbrycrd_keys(val)

    elif isinstance(obj, list):
        obj = [ format_lbrycrd_keys(o) for o in obj ]
    return obj

# Format amount to be decimal encoded string
def format_amount(obj):
    if isinstance(obj, dict):
        for k, v in obj.iteritems():
            if k == 'amount' or k == 'effective_amount':
                obj[k] = str(Decimal(obj[k])/COIN)
            if isinstance(v, list) or isinstance(v, dict):
                obj[k] = format_amount(v)
    elif isinstance(obj, list):
        obj = [ format_amount(o) for o in obj ]
    return obj


class Command:

    def __init__(self, func, s):
        self.name = func.__name__
        self.requires_network = 'n' in s
        self.requires_wallet = 'w' in s
        self.requires_password = 'p' in s
        self.description = func.__doc__
        self.help = self.description.split('.')[0] if self.description else None
        varnames = func.func_code.co_varnames[1:func.func_code.co_argcount]
        self.defaults = func.func_defaults
        if self.defaults:
            n = len(self.defaults)
            self.params = list(varnames[:-n])
            self.options = list(varnames[-n:])
        else:
            self.params = list(varnames)
            self.options = []
            self.defaults = []


def command(s):
    def decorator(func):
        global known_commands
        name = func.__name__
        known_commands[name] = Command(func, s)
        @wraps(func)
        def func_wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return func_wrapper
    return decorator


class Commands:

    def __init__(self, config, wallet, network, callback = None, password=None, new_password=None):
        self.config = config
        self.wallet = wallet
        self.network = network
        self._callback = callback
        self._password = password
        self.new_password = new_password
        self.contacts = contacts.Contacts(self.config)

    def _run(self, method, args, password_getter):
        cmd = known_commands[method]
        if cmd.requires_password and self.wallet.use_encryption:
            self._password = apply(password_getter,())
        f = getattr(self, method)
        result = f(*args)
        self._password = None
        if self._callback:
            apply(self._callback, ())
        return result

    @command('')
    def commands(self):
        """List of commands"""
        return ' '.join(sorted(known_commands.keys()))

    @command('')
    def create(self):
        """Create a new wallet"""
        raise BaseException('Not a JSON-RPC command')

    @command('wn')
    def restore(self, text):
        """Restore a wallet from text. Text can be a seed phrase, a master
        public key, a master private key, a list of bitcoin addresses
        or bitcoin private keys. If you want to be prompted for your
        seed, type '?' or ':' (concealed) """
        raise BaseException('Not a JSON-RPC command')

    @command('w')
    def deseed(self):
        """Remove seed from wallet. This creates a seedless, watching-only
        wallet."""
        raise BaseException('Not a JSON-RPC command')

    @command('wp')
    def password(self):
        """Change wallet password. """
        self.wallet.update_password(self._password, self.new_password)
        self.wallet.storage.write()
        return {'password':self.wallet.use_encryption}

    @command('')
    def getconfig(self, key):
        """Return a configuration variable. """
        return self.config.get(key)

    @command('')
    def setconfig(self, key, value):
        """Set a configuration variable. 'value' may be a string or a Python expression."""
        try:
            value = ast.literal_eval(value)
        except:
            pass
        self.config.set_key(key, value)
        return True

    @command('')
    def make_seed(self, nbits=128, entropy=1, language=None):
        """Create a seed"""
        from mnemonic import Mnemonic
        s = Mnemonic(language).make_seed(nbits, custom_entropy=entropy)
        return s.encode('utf8')

    @command('')
    def check_seed(self, seed, entropy=1, language=None):
        """Check that a seed was generated with given entropy"""
        from mnemonic import Mnemonic
        return Mnemonic(language).check_seed(seed, entropy)

    @command('n')
    def getaddresshistory(self, address):
        """Return the transaction history of any address. Note: This is a
        walletless server query, results are not checked by SPV.
        """
        return self.network.synchronous_get(('blockchain.address.get_history', [address]))

    @command('w')
    def listunspent(self):
        """List unspent outputs. Returns the list of unspent transaction
        outputs in your wallet."""
        l = copy.deepcopy(self.wallet.get_spendable_coins(exclude_frozen = False))
        for i in l:
            v = i["value"]
            i["value"] = float(v)/COIN if v is not None else None
        return l

    @command('n')
    def getaddressunspent(self, address):
        """Returns the UTXO list of any address. Note: This
        is a walletless server query, results are not checked by SPV.
        """
        return self.network.synchronous_get(('blockchain.address.listunspent', [address]))

    @command('n')
    def getutxoaddress(self, txid, pos):
        """Get the address of a UTXO. Note: This is a walletless server query, results are
        not checked by SPV.
        """
        r = self.network.synchronous_get(('blockchain.utxo.get_address', [txid, pos]))
        return {'address': r}

    @command('wp')
    def createrawtx(self, inputs, outputs, unsigned=False):
        """Create a transaction from json inputs. The syntax is similar to bitcoind."""
        coins = self.wallet.get_spendable_coins(exclude_frozen = False)
        tx_inputs = []
        for i in inputs:
            prevout_hash = i['txid']
            prevout_n = i['vout']
            for c in coins:
                if c['prevout_hash'] == prevout_hash and c['prevout_n'] == prevout_n:
                    self.wallet.add_input_info(c)
                    tx_inputs.append(c)
                    break
            else:
                raise BaseException('Transaction output not in wallet', prevout_hash+":%d"%prevout_n)
        outputs = map(lambda x: (TYPE_ADDRESS, x[0], int(COIN*x[1])), outputs.items())
        tx = Transaction.from_io(tx_inputs, outputs)
        if not unsigned:
            self.wallet.sign_transaction(tx, self._password)
        return tx.as_dict()

    @command('wp')
    def signtransaction(self, tx, privkey=None):
        """Sign a transaction. The wallet keys will be used unless a private key is provided."""
        t = Transaction(tx)
        if privkey:
            pubkey = lbrycrd.public_key_from_private_key(privkey)
            t.sign({pubkey:privkey})
        else:
            self.wallet.sign_transaction(t, self._password)
        return t.as_dict()

    @command('')
    def deserialize(self, tx):
        """Deserialize a serialized transaction"""
        return Transaction(tx).deserialize()

    @command('n')
    def broadcast(self, tx):
        """Broadcast a transaction to the network. """
        t = Transaction(tx)
        return self.network.synchronous_get(('blockchain.transaction.broadcast', [str(t)]))

    @command('')
    def createmultisig(self, num, pubkeys):
        """Create multisig address"""
        assert isinstance(pubkeys, list), (type(num), type(pubkeys))
        redeem_script = Transaction.multisig_script(pubkeys, num)
        address = hash_160_to_bc_address(hash_160(redeem_script.decode('hex')), 5)
        return {'address':address, 'redeemScript':redeem_script}

    @command('w')
    def freeze(self, address):
        """Freeze address. Freeze the funds at one of your wallet\'s addresses"""
        return self.wallet.set_frozen_state([address], True)

    @command('w')
    def unfreeze(self, address):
        """Unfreeze address. Unfreeze the funds at one of your wallet\'s address"""
        return self.wallet.set_frozen_state([address], False)

    @command('wp')
    def getprivatekeys(self, address):
        """Get private keys of addresses. You may pass a single wallet address, or a list of wallet addresses."""
        is_list = type(address) is list
        domain = address if is_list else [address]
        out = [self.wallet.get_private_key(address, self._password) for address in domain]
        return out if is_list else out[0]

    @command('w')
    def ismine(self, address):
        """Check if address is in wallet. Return true if and only address is in wallet"""
        return self.wallet.is_mine(address)

    @command('')
    def dumpprivkeys(self):
        """Deprecated."""
        return "This command is deprecated. Use a pipe instead: 'lbryum listaddresses | lbryum getprivatekeys - '"

    @command('')
    def validateaddress(self, address):
        """Check that an address is valid. """
        return is_address(address)

    @command('w')
    def getpubkeys(self, address):
        """Return the public keys for a wallet address. """
        return self.wallet.get_public_keys(address)

    @command('w')
    def getbalance(self, account=None, exclude_claimtrietx=False):
        """Return the balance of your wallet. """
        if account is None:
            c, u, x = self.wallet.get_balance(exclude_claimtrietx=exclude_claimtrietx)
        else:
            c, u, x = self.wallet.get_account_balance(account,exclude_claimtrietx)
        out = {"confirmed": str(Decimal(c)/COIN)}
        if u:
            out["unconfirmed"] = str(Decimal(u)/COIN)
        if x:
            out["unmatured"] = str(Decimal(x)/COIN)
        return out

    @command('n')
    def getaddressbalance(self, address):
        """Return the balance of any address. Note: This is a walletless
        server query, results are not checked by SPV.
        """
        out = self.network.synchronous_get(('blockchain.address.get_balance', [address]))
        out["confirmed"] =  str(Decimal(out["confirmed"])/COIN)
        out["unconfirmed"] =  str(Decimal(out["unconfirmed"])/COIN)
        return out

    @command('n')
    def getproof(self, address):
        """Get Merkle branch of an address in the UTXO set"""
        p = self.network.synchronous_get(('blockchain.address.get_proof', [address]))
        out = []
        for i,s in p:
            out.append(i)
        return out

    @command('n')
    def getmerkle(self, txid, height):
        """Get Merkle branch of a transaction included in a block. Electrum
        uses this to verify transactions (Simple Payment Verification)."""
        return self.network.synchronous_get(('blockchain.transaction.get_merkle', [txid, int(height)]))

    @command('n')
    def getservers(self):
        """Return the list of available servers"""
        while not self.network.is_up_to_date():
            time.sleep(0.1)
        return self.network.get_servers()

    @command('')
    def version(self):
        """Return the version of lbryum."""
        import lbryum  # Needs to stay here to prevent ciruclar imports
        return lbryum.LBRYUM_VERSION

    @command('w')
    def getmpk(self):
        """Get master public key. Return your wallet\'s master public key(s)"""
        return self.wallet.get_master_public_keys()

    @command('wp')
    def getmasterprivate(self):
        """Get master private key. Return your wallet\'s master private key"""
        return str(self.wallet.get_master_private_key(self.wallet.root_name, self._password))

    @command('wp')
    def getseed(self):
        """Get seed phrase. Print the generation seed of your wallet."""
        s = self.wallet.get_mnemonic(self._password)
        return s.encode('utf8')

    @command('wp')
    def importprivkey(self, privkey):
        """Import a private key. """
        try:
            addr = self.wallet.import_key(privkey, self._password)
            out = "Keypair imported: " + addr
        except Exception as e:
            out = "Error: " + str(e)
        return out

    def _resolver(self, x):
        if x is None:
            return None
        out = self.contacts.resolve(x)
        if out.get('type') == 'openalias' and self.nocheck is False and out.get('validated') is False:
            raise BaseException('cannot verify alias', x)
        return out['address']

    @command('n')
    def sweep(self, privkey, destination, tx_fee=None, nocheck=False):
        """Sweep private keys. Returns a transaction that spends UTXOs from
        privkey to a destination address. The transaction is not
        broadcasted."""
        privkeys = privkey if type(privkey) is list else [privkey]
        self.nocheck = nocheck
        dest = self._resolver(destination)
        if tx_fee is None:
            tx_fee = 0.0001
        fee = int(Decimal(tx_fee)*COIN)
        return Transaction.sweep(privkeys, self.network, dest, fee)

    @command('wp')
    def signmessage(self, address, message):
        """Sign a message with a key. Use quotes if your message contains
        whitespaces"""
        sig = self.wallet.sign_message(address, message, self._password)
        return base64.b64encode(sig)

    @command('')
    def verifymessage(self, address, signature, message):
        """Verify a signature."""
        sig = base64.b64decode(signature)
        return lbrycrd.verify_message(address, sig, message)

    def _mktx(self, outputs, fee, change_addr, domain, nocheck, unsigned, claim_name=None, claim_val=None,
              abandon_txid=None, claim_id=None):
        self.nocheck = nocheck
        change_addr = self._resolver(change_addr)
        domain = None if domain is None else map(self._resolver, domain)
        fee = None if fee is None else int(COIN*Decimal(fee))
        final_outputs = []
        for address, amount in outputs:
            address = self._resolver(address)
            #assert self.wallet.is_mine(address)
            if amount == '!':
                assert len(outputs) == 1
                inputs = self.wallet.get_spendable_coins(domain)
                amount = sum(map(lambda x:x['value'], inputs))
                if fee is None:
                    for i in inputs:
                        self.wallet.add_input_info(i)
                    output = (TYPE_ADDRESS, address, amount)
                    dummy_tx = Transaction.from_io(inputs, [output])
                    fee_per_kb = self.wallet.fee_per_kb(self.config)
                    fee = dummy_tx.estimated_fee(fee_per_kb)
                amount -= fee
            else:
                amount = int(COIN*Decimal(amount))
            txout_type = TYPE_ADDRESS
            val = address
            if claim_name is not None and claim_val is not None and claim_id is not None and abandon_txid is not None:
                assert len(outputs) == 1
                txout_type |= TYPE_UPDATE
                val = ((claim_name, claim_id, claim_val), val)
            elif claim_name is not None and claim_id is not None:
                assert len(outputs) == 1
                txout_type |= TYPE_SUPPORT
                val = ((claim_name, claim_id), val)
            elif claim_name is not None and claim_val is not None:
                assert len(outputs) == 1
                txout_type |= TYPE_CLAIM
                val = ((claim_name, claim_val), val)
            final_outputs.append((txout_type, val, amount))

        coins = self.wallet.get_spendable_coins(domain, abandon_txid=abandon_txid)
        tx = self.wallet.make_unsigned_transaction(coins, final_outputs, self.config, fee, change_addr,
                                                   abandon_txid=abandon_txid)
        str(tx) #this serializes
        if not unsigned:
            self.wallet.sign_transaction(tx, self._password)

        return tx

    @command('wp')
    def payto(self, destination, amount, tx_fee=None, from_addr=None, change_addr=None, nocheck=False, unsigned=False):
        """Create a raw transaction. """
        domain = [from_addr] if from_addr else None
        tx = self._mktx([(destination, amount)], tx_fee, change_addr, domain, nocheck, unsigned)
        return tx.as_dict()

    @command('wpn')
    def paytoandsend(self, destination, amount, tx_fee=None, from_addr=None, change_addr=None, nocheck=False, unsigned=False):
        """Create and broadcast transaction. """
        domain = [from_addr] if from_addr else None
        tx = self._mktx([(destination, amount)], tx_fee, change_addr, domain, nocheck, unsigned)
        return self.network.synchronous_get(('blockchain.transaction.broadcast', [str(tx)]))

    @command('wp')
    def paytomany(self, outputs, tx_fee=None, from_addr=None, change_addr=None, nocheck=False, unsigned=False):
        """Create a multi-output transaction. """
        domain = [from_addr] if from_addr else None
        tx = self._mktx(outputs, tx_fee, change_addr, domain, nocheck, unsigned)
        return tx.as_dict()

    @command('wp')
    def paytomanyandsend(self, outputs, tx_fee=None, from_addr=None, change_addr=None, nocheck=False, unsigned=False):
        """Create and broadcast a multi-output transaction. """
        domain = [from_addr] if from_addr else None
        tx = self._mktx(outputs, tx_fee, change_addr, domain, nocheck, unsigned)
        return self.network.synchronous_get(('blockchain.transaction.broadcast', [str(tx)]))

    @command('w')
    def history(self):
        """Wallet history. Returns the transaction history of your wallet."""
        balance = 0
        out = []
        for item in self.wallet.get_history():
            tx_hash, conf, value, timestamp, balance = item
            try:
                time_str = datetime.datetime.fromtimestamp(timestamp).isoformat(' ')[:-3]
            except Exception:
                time_str = "----"
            label = self.wallet.get_label(tx_hash)
            out.append({
                'txid':tx_hash,
                'timestamp':timestamp,
                'date':"%16s"%time_str,
                'label':label,
                'value':float(value)/COIN if value is not None else None,
                'confirmations':conf}
            )
        return out

    @command('w')
    def setlabel(self, key, label):
        """Assign a label to an item. Item may be a bitcoin address or a
        transaction ID"""
        self.wallet.set_label(key, label)

    @command('')
    def listcontacts(self):
        """Show your list of contacts"""
        return self.contacts

    @command('')
    def getalias(self, key):
        """Retrieve alias. Lookup in your list of contacts, and for an OpenAlias DNS record."""
        return self.contacts.resolve(key)

    @command('')
    def searchcontacts(self, query):
        """Search through contacts, return matching entries. """
        results = {}
        for key, value in self.contacts.items():
            if query.lower() in key.lower():
                results[key] = value
        return results

    @command('w')
    def listaddresses(self, receiving=False, change=False, show_labels=False, frozen=False, unused=False, funded=False, show_balance=False):
        """List wallet addresses. Returns the list of all addresses in your wallet. Use optional arguments to filter the results."""
        out = []
        for addr in self.wallet.addresses(True):
            if frozen and not self.wallet.is_frozen(addr):
                continue
            if receiving and self.wallet.is_change(addr):
                continue
            if change and not self.wallet.is_change(addr):
                continue
            if unused and self.wallet.is_used(addr):
                continue
            if funded and self.wallet.is_empty(addr):
                continue
            item = addr
            if show_balance:
                item += ", "+ format_satoshis(sum(self.wallet.get_addr_balance(addr)))
            if show_labels:
                item += ', ' + repr(self.wallet.labels.get(addr, ''))
            out.append(item)
        return out

    @command('w')
    def gettransaction(self, txid):
        """Retrieve a transaction. """
        tx = self.wallet.transactions.get(txid) if self.wallet else None
        if tx is None and self.network:
            raw = self.network.synchronous_get(('blockchain.transaction.get', [txid]))
            if raw:
                tx = Transaction(raw)
            else:
                raise BaseException("Unknown transaction")
        return tx.as_dict()

    @command('')
    def encrypt(self, pubkey, message):
        """Encrypt a message with a public key. Use quotes if the message contains whitespaces."""
        return lbrycrd.encrypt_message(message, pubkey)

    @command('wp')
    def decrypt(self, pubkey, encrypted):
        """Decrypt a message encrypted with a public key."""
        return self.wallet.decrypt_message(pubkey, encrypted, self._password)

    def _format_request(self, out):
        pr_str = {
            PR_UNKNOWN: 'Unknown',
            PR_UNPAID: 'Pending',
            PR_PAID: 'Paid',
            PR_EXPIRED: 'Expired',
        }
        out['amount (BTC)'] = format_satoshis(out.get('amount'))
        out['status'] = pr_str[out.get('status', PR_UNKNOWN)]
        return out

    @command('w')
    def getrequest(self, key):
        """Return a payment request"""
        r = self.wallet.get_payment_request(key, self.config)
        if not r:
            raise BaseException("Request not found")
        return self._format_request(r)

    #@command('w')
    #def ackrequest(self, serialized):
    #    """<Not implemented>"""
    #    pass

    @command('w')
    def listrequests(self, pending=False, expired=False, paid=False):
        """List the payment requests you made."""
        out = self.wallet.get_sorted_requests(self.config)
        if pending:
            f = PR_UNPAID
        elif expired:
            f = PR_EXPIRED
        elif paid:
            f = PR_PAID
        else:
            f = None
        if f is not None:
            out = filter(lambda x: x.get('status')==f, out)
        return map(self._format_request, out)

    @command('w')
    def addrequest(self, amount, memo='', expiration=60*60, force=False):
        """Create a payment request."""
        addr = self.wallet.get_unused_address(None)
        if addr is None:
            if force:
                addr = self.wallet.create_new_address(None, False)
            else:
                return False
        amount = int(COIN*Decimal(amount))
        expiration = int(expiration)
        req = self.wallet.make_payment_request(addr, amount, memo, expiration)
        self.wallet.add_payment_request(req, self.config)
        out = self.wallet.get_payment_request(addr, self.config)
        return self._format_request(out)

    @command('wp')
    def signrequest(self, address):
        "Sign payment request with an OpenAlias"
        alias = self.config.get('alias')
        if not alias:
            raise BaseException('No alias in your configuration')
        alias_addr = self.contacts.resolve(alias)['address']
        self.wallet.sign_payment_request(address, alias, alias_addr, self._password)

    @command('w')
    def rmrequest(self, address):
        """Remove a payment request"""
        return self.wallet.remove_payment_request(address, self.config)

    @command('w')
    def clearrequests(self):
        """Remove all payment requests"""
        for k in self.wallet.receive_requests.keys():
            self.wallet.remove_payment_request(k, self.config)

    @command('n')
    def notify(self, address, URL):
        """Watch an address. Everytime the address changes, a http POST is sent to the URL."""
        def callback(x):
            import urllib2
            headers = {'content-type':'application/json'}
            data = {'address':address, 'status':x.get('result')}
            try:
                req = urllib2.Request(URL, json.dumps(data), headers)
                response_stream = urllib2.urlopen(req)
                util.print_error('Got Response for %s' % address)
            except BaseException as e:
                util.print_error(str(e))
        self.network.send([('blockchain.address.subscribe', [address])], callback)
        return True

    @staticmethod
    def _verify_proof(name, claim_trie_root, result):
        """ Verify proof for name claim """
        def _build_response(value, txid, n, amount, height):
            r = {
                    'value': value,
                    'txid': txid,
                    'nout': n,
                    'amount': str(Decimal(amount)/COIN),
                    'height': height
                }
            return r

        def _parse_proof_result(name, result):
            support_amount = sum(samount for stxid, sn, samount in result['supports'])
            if 'txhash' in result['proof'] and 'nOut' in result['proof']:
                if 'transaction' in result:
                    computed_txhash = Hash(result['transaction'].decode('hex'))[::-1].encode('hex')
                    tx = deserialize_transaction(result['transaction'])
                    nOut = result['proof']['nOut']
                    if result['proof']['txhash'] == computed_txhash:
                        if 0 <= nOut < len(tx['outputs']):
                            scriptPubKey = tx['outputs'][nOut]['scriptPubKey']
                            amount = tx['outputs'][nOut]['value']
                            effective_amount = amount + support_amount
                            h = tx['lockTime'] + 1
                            decoded_script = [r for r in script_GetOp(scriptPubKey.decode('hex'))]
                            decode_out = decode_claim_script(decoded_script)
                            if decode_out is False:
                                return {'error': 'failed to decode as claim script'}
                            n,script = decode_out
                            decoded_name, decoded_value = n.name, n.value
                            if decoded_name == name:
                                return _build_response(decoded_value, computed_txhash, nOut, effective_amount, h)
                            return {'error': 'name in proof did not match requested name'}
                        return {'error': 'invalid nOut: %d (let(outputs): %d' % (nOut, len(tx['outputs']))}
                    return {'error': "computed txid did not match given transaction: %s vs %s" %
                                     (computed_txhash, result['proof']['txhash'])
                    }
                return {'error': "didn't receive a transaction with the proof"}
            return {'error':'name is not claimed'}


        if 'proof' in result:
            try:
                verify_proof(result['proof'], claim_trie_root, name)
            except InvalidProofError:
                return {'error': "Proof was invalid"}
            return _parse_proof_result(name, result)
        else:
            return {'error': "proof not in result"}

    @command('n')
    def requestvalueforname(self, name, blockhash):
        """Request and return value of name with proof from lbryum server without verifying proof"""
        log.info('Requesting value for name: %s, blockhash: %s', name, blockhash)
        return self.network.synchronous_get(('blockchain.claimtrie.getvalue', [name, blockhash]))

    @command('n')
    def getvalueforname(self, name):
        """Request value of name from lbryum server and verify its proof"""
        block_header = self.network.blockchain.read_header(
            self.network.get_local_height() - RECOMMENDED_CLAIMTRIE_HASH_CONFIRMS + 1)
        block_hash = self.network.blockchain.hash_header(block_header)
        response = self.requestvalueforname(name, block_hash)
        return Commands._verify_proof(name, block_header['claim_trie_root'], response)

    @command('n')
    def getclaimsfromtx(self, txid):
        """Return the claims which are in a transaction"""
        out = self.network.synchronous_get(('blockchain.claimtrie.getclaimsintx', [txid]))
        return format_amount(format_lbrycrd_keys(out))

    @command('n')
    def getclaimsforname(self, name):
        """Return all claims and supports for a name"""
        out = self.network.synchronous_get(('blockchain.claimtrie.getclaimsforname', [name]))
        return format_amount(format_lbrycrd_keys(out))

    @command('n')
    def getblock(self, blockhash):
        """Return a block matching the given blockhash"""
        return self.network.synchronous_get(('blockchain.block.get_block', [blockhash]))

    @command('n')
    def getclaimtrie(self):
        """Return the entire claim trie"""
        return self.network.synchronous_get(('blockchain.claimtrie.get', []))
    """
    get claim by claim id
    Args:
    claim_id : claim id of claim

    Output:
    None if no claim exists with the claim id, otherwise return dict with below contents
    claimId: claim id of claim
    depth: block height of claim
    in claim trie: True if found in claimtrie
    is controlling: True if controlling claim
    nOut: nout of claim
    name: name of claim
    value: value of claim
    """
    @command('n')
    def getclaimbyid(self, claim_id):
        return self.network.synchronous_get(('blockchain.claimtrie.getclaimbyid', [claim_id]))

    @command('w')
    def getnameclaims(self):
        return self.wallet.get_name_claims()

    @command('wp')
    def claimname(self, destination, amount, name, val, tx_fee=None, from_addr=None, change_addr=None,
                  nocheck=False, unsigned=False):
        """Claim a name."""
        domain = [from_addr] if from_addr else None
        tx = self._mktx([(destination, amount)], tx_fee, change_addr, domain, nocheck, unsigned,
                        claim_name=name, claim_val=val)
        return tx.as_dict()

    @command('wp')
    def supportclaim(self, destination, amount, name, claim_id, tx_fee=None, from_addr=None,
                     change_addr=None, nocheck=False, unsigned=False):
        """Support a claim"""
        domain = [from_addr] if from_addr else None
        tx = self._mktx([(destination, amount)], tx_fee, change_addr, domain, nocheck, unsigned,
                        claim_name=name, claim_id=claim_id)
        return tx.as_dict()

    @command('wp')
    def updateclaim(self, txid, destination, amount, name, claim_id, val, tx_fee=None,
                    change_addr=None, nocheck=False, unsigned=False):
        """Update a claim"""
        domain = None
        tx = self._mktx([(destination, amount)], tx_fee, change_addr, domain, nocheck, unsigned,
                        claim_name=name, claim_id=claim_id, claim_val=val, abandon_txid=txid)
        return tx.as_dict()

    @command('wp')
    def abandonclaim(self, txid, destination, amount, tx_fee=None, change_addr=None,
                     nocheck=False, unsigned=False):
        domain = None
        tx = self._mktx([(destination, amount)], tx_fee, change_addr, domain, nocheck, unsigned,
                        abandon_txid=txid)
        return tx.as_dict()

    def _calculate_fee(self,inputs,outputs,set_tx_fee):
        if set_tx_fee is not None:
            return set_tx_fee
        dummy_tx = Transaction.from_io(inputs, outputs)
        # fee per kb will default to RECOMMENDED_FEE, which is 50000
        # relay fee will default to 5000
        # fee is max(relay_fee, size is fee_per_kb * esimated_size)
        # will be roughly 10,000 deweys (0.0001 lbc), standard abandon should be about 200 bytes
        # this is assuming config is not set to dynamic, which in case it will get fees from lbrycrd's
        # fee estimation algorithm
        size = dummy_tx.estimated_size()
        fee = Transaction.fee_for_size(self.wallet.relayfee(),self.wallet.fee_per_kb(self.config),size)
        return fee

    """
    name claim
    Args:
    name : name to claim
    val : value the name is set to
    amount : amount to claim
    broadcast [default = True]: if True, broadcast transaction to the network
    claim_addr [optional] : address where claim will be sent
    tx_fee [optional] : transaction fee
    change_addr [optional] : address where change amount will be sent

    Output:
    success : True if succesful , False otherwise
    reason : if not succesful, give reason
    txid : txid of resulting transaction if succesful
    nout : nout of the resulting support claim if succesful
    tx: raw tx of the resulting transaction
    fee : fee paid for the transaction if succesful
    claim_id : claim id of the resulting transaction
    """

    @command('wpn')
    def claim(self, name, val, amount, broadcast=True, claim_addr=None, tx_fee=None, change_addr=None):
        if claim_addr is None:
            claim_addr = self.wallet.create_new_address()
        if change_addr is None:
            change_addr = self.wallet.create_new_address(for_change=True)
        amount = int(COIN*amount)
        if amount <= 0:
            return {'success':False,'reason':'Amount must be greater than 0'}
        if tx_fee is not None:
            tx_fee = int(COIN*tx_fee)
            if tx_fee < 0:
                return {'success':False,'reason':'tx_fee must be greater than or equal to 0'}

        outputs = [(TYPE_ADDRESS | TYPE_CLAIM,((name,val),claim_addr),amount)]
        coins = self.wallet.get_spendable_coins()
        try:
            tx = self.wallet.make_unsigned_transaction(coins,outputs,self.config,tx_fee,change_addr)
        except NotEnoughFunds:
            return {'success':False, 'reason':'Not enough funds'}
        self.wallet.sign_transaction(tx, self._password)
        if broadcast:
            success,out = self.wallet.sendtx(tx)
            if not success:
                return {'success':False,'reason':out}

        nout = None
        for i,output in enumerate(tx._outputs):
            if output[0] & TYPE_CLAIM:
                nout = i
        assert(nout is not None)

        claimid = lbrycrd.encode_claim_id_hex(lbrycrd.claim_id_hash(lbrycrd.rev_hex(tx.hash()).decode('hex'),nout))
        return {"success":True,"txid":tx.hash(),"nout":nout,"tx":str(tx),"fee":str(Decimal(tx.get_fee())/COIN),
                "claim_id":claimid}
    """
    support claim
    Args:
    name : name of claim to support
    claim_id : claim id of claim to support
    amount : amount to support
    broadcast [default = True]: if True, broadcast transaction to the network
    claim_addr [optional] : address where support claim will be sent
    tx_fee [optional] : transaction fee
    change_addr [optional] : address where change amount will be sent

    Output:
    success : True if succesful , False otherwise
    reason : if not succesful, give reason
    txid : txid of resulting transaction if succesful
    nout : nout of the resulting support claim if succesful
    tx: raw tx of the resulting transaction
    fee : fee paid for the transaction if succesful
    """

    @command('wpn')
    def support(self, name, claim_id, amount, broadcast=True, claim_addr=None, tx_fee=None,
                     change_addr=None):
        if claim_addr is None:
            claim_addr = self.wallet.create_new_address()
        if change_addr is None:
            change_addr = self.wallet.create_new_address(for_change=True)

        claim_id = lbrycrd.decode_claim_id_hex(claim_id)
        amount = int(COIN*amount)
        if amount <= 0:
            return {'success':False,'reason':'Amount must be greater than 0'}
        if tx_fee is not None:
            tx_fee = int(COIN*tx_fee)
            if tx_fee < 0:
                return {'success':False,'reason':'tx_fee must be greater than or equal to 0'}

        outputs = [(TYPE_ADDRESS | TYPE_SUPPORT,((name,claim_id),claim_addr),amount)]
        coins = self.wallet.get_spendable_coins()
        try:
            tx = self.wallet.make_unsigned_transaction(coins,outputs,self.config,tx_fee,change_addr)
        except NotEnoughFunds:
            return {'success':False, 'reason':'Not enough funds'}
        self.wallet.sign_transaction(tx, self._password)
        if broadcast:
            success,out = self.wallet.sendtx(tx)
            if not success:
                return {'success':False,'reason':out}

        nout = None
        for i,output in enumerate(tx._outputs):
            if output[0] & TYPE_SUPPORT:
                nout = i

        return {"success":True,"txid":tx.hash(),"nout":nout,"tx":str(tx),"fee":str(Decimal(tx.get_fee())/COIN)}

    """
    update claim
    Args:
    txid : txid of claim to udpate
    nout : nout of claim to update
    name : name of claim to update
    claim_id : claim id of claim to update
    val : value to update to
    amount : amount to update to, if set to None, will be the current claim amount - tx_fee
    broadcast [default = True]: if True, broadcast transaction to the network
    claim_addr [optional] : address where claim will be sent
    tx_fee [optional] : transaction fee
    change_addr [optional] : address where change amount is sent

    Output:
    success : True if succesful , False otherwise
    reason : if not succesful, give reason
    txid : txid of resulting transaction if succesful
    nout : nout of the resulting claim update if succesful
    tx: raw tx of the resulting transaction
    fee : fee paid for the transaction if succesful
    amount: amount updated to
    """

    @command('wpn')
    def update(self, txid, nout, name, claim_id, val, amount, broadcast=True, claim_addr=None, tx_fee=None,
                    change_addr=None):

        if claim_addr is None:
            claim_addr = self.wallet.create_new_address()
        if change_addr is None:
            change_addr = self.wallet.create_new_address(for_change=True)

        claim_id = lbrycrd.decode_claim_id_hex(claim_id)

        if amount is not None:
            amount = int(COIN*amount)
            if amount <= 0:
                return {'success':False,'reason':'Amount must be greater than 0'}
        if tx_fee is not None:
            tx_fee = int(COIN*tx_fee)
            if tx_fee < 0:
                return {'success':False,'reason':'tx_fee must be greater than or equal to 0'}

        claim_utxo = self.wallet.get_spendable_claimtrietx_coin(txid,nout)
        if claim_utxo['is_support']:
            return {'success':False,'reason':'Cannot update a support'}

        inputs = [claim_utxo]
        txout_value = claim_utxo['value']

        # if amount is not specified, keep the same amount minus the tx fee
        if amount is None:
            dummy_outputs = [(TYPE_ADDRESS | TYPE_UPDATE,((name,claim_id,val),claim_addr),txout_value)]
            fee = self._calculate_fee(inputs,dummy_outputs,tx_fee)
            if fee >= txout_value:
                return {'success':False,'reason':'Fee will exceed amount available in original bid. Increase amount'}
            outputs = [(TYPE_ADDRESS | TYPE_UPDATE,((name,claim_id,val),claim_addr),txout_value - fee)]

        elif amount <= 0:
            return {'success':False,'reason':'Amount must be greater than zero'}

        # amount is more than the original bid or equal, we need to get an input
        elif amount >= txout_value:
            additional_input_fee = 0
            if tx_fee is None:
                claim_input_size = Transaction.estimated_input_size(claim_utxo)
                additional_input_fee = Transaction.fee_for_size(self.wallet.relayfee(),
                                                                self.wallet.fee_per_kb(self.config),
                                                                claim_input_size)

            get_inputs_for_amount = amount - txout_value + additional_input_fee
            # create a dummy tx for the extra amount in order to get the proper inputs to spend
            dummy_outputs = [(TYPE_ADDRESS | TYPE_UPDATE,((name,claim_id,val),claim_addr),get_inputs_for_amount)]
            coins = self.wallet.get_spendable_coins()
            try:
                dummy_tx = self.wallet.make_unsigned_transaction(coins,dummy_outputs,self.config,tx_fee,change_addr)
            except NotEnoughFunds:
                return {'success':False, 'reason':'Not enough funds'}

            # add the unspents to input
            for i in dummy_tx._inputs:
                inputs.append(i)

            outputs = [(TYPE_ADDRESS | TYPE_UPDATE,((name,claim_id,val),claim_addr),amount)]
            # add the change utxos to output
            for output in  dummy_tx._outputs:
                if not (output[0] & TYPE_UPDATE):
                    outputs.append(output)

        # amount is less than the original bid, we need to put remainder minus fees in a change address
        elif amount < txout_value:

            dummy_outputs = [(TYPE_ADDRESS | TYPE_UPDATE,((name,claim_id,val),claim_addr),amount),
                             (TYPE_ADDRESS,change_addr,txout_value-amount)]
            fee = self._calculate_fee(inputs,dummy_outputs,tx_fee)
            if fee > txout_value-amount:
                return {"success":False,'reason':'Fee will be greater than change amount, use amount=None to expend change as fee'}

            outputs = [(TYPE_ADDRESS | TYPE_UPDATE,((name,claim_id,val),claim_addr),amount),
                       (TYPE_ADDRESS,change_addr,txout_value-amount-fee)]

        tx = Transaction.from_io(inputs,outputs)
        self.wallet.sign_transaction(tx, self._password)
        if broadcast:
            success,out = self.wallet.sendtx(tx)
            if not success:
                return {"success":False, "reason":out}

        nout = None
        amount = 0
        for i,output in enumerate(tx._outputs):
            if output[0] & TYPE_UPDATE:
                nout = i
                amount = output[2]

        return {"success":True,"txid":tx.hash(),"nout":nout,"tx":str(tx),"fee":str(Decimal(tx.get_fee())/COIN),
                "amount":str(Decimal(amount)/COIN),}

    """
    abandon claim
    Args:
    txid : txid of claim to abandon
    nout : nout of claim to abandon
    broadcast [default = True]: if True, broadcast transaction to the network
    return_addr [optional] : address where amount will be returned
    tx_fee [optional] : transaction fee

    Output:
    success : True if succesful , False otherwise
    reason : if not succesful, give reason
    txid : txid of resulting transaction if succesful
    tx: raw tx of the resulting transaction
    fee : fee paid for the transaction if succesful
    """
    @command('wpn')
    def abandon(self, txid, nout, broadcast=True, return_addr=None, tx_fee=None):
        # create a single new address to abandon into if return_addr was not specified
        if return_addr is None:
            return_addr = self.wallet.create_new_address()
        if tx_fee is not None:
            tx_fee = int(COIN*tx_fee)
            if tx_fee < 0:
                return {'success':False,'reason':'tx_fee must be greater than or equal to 0'}

        i = self.wallet.get_spendable_claimtrietx_coin(txid,nout)
        inputs = [i]
        txout_value = i['value']
        # create outputs
        outputs = [(TYPE_ADDRESS,return_addr,txout_value)]
        # fee will be roughly 10,000 deweys (0.0001 lbc), standard abandon should be about 200 bytes
        # this is assuming config is not set to dynamic, which in case it will get fees from lbrycrd's
        # fee estimation algorithm
        fee = self._calculate_fee(inputs,outputs,tx_fee)
        if fee > txout_value:
            return {'success':False,'reason':'transaction fee exceeds amount to abandon'}
        return_value = txout_value - fee

        # create transaction
        outputs = [(TYPE_ADDRESS,return_addr,return_value)]
        tx = Transaction.from_io(inputs,outputs)
        self.wallet.sign_transaction(tx, self._password)
        if broadcast:
            success,out = self.wallet.sendtx(tx)
            if not success:
                return {'success':False,'reason':out}
        return {'success':True,'txid':tx.hash(),'tx':str(tx),'fee':str(Decimal(tx.get_fee())/COIN)}

param_descriptions = {
    'privkey': 'Private key. Type \'?\' to get a prompt.',
    'destination': 'Bitcoin address, contact or alias',
    'address': 'Bitcoin address',
    'seed': 'Seed phrase',
    'txid': 'Transaction ID',
    'pos': 'Position',
    'height': 'Block height',
    'tx': 'Serialized transaction (hexadecimal)',
    'key': 'Variable name',
    'pubkey': 'Public key',
    'message': 'Clear text message. Use quotes if it contains spaces.',
    'encrypted': 'Encrypted message',
    'amount': 'Amount to be sent (in BTC). Type \'!\' to send the maximum available.',
    'requested_amount': 'Requested amount (in BTC).',
    'outputs': 'list of ["address", amount]',
    'exclude_claimtrietx': 'Exclude claimtrie transactions.',
}

command_options = {
    'password':    ("-W", "--password",    "Password"),
    'receiving':   (None, "--receiving",   "Show only receiving addresses"),
    'change':      (None, "--change",      "Show only change addresses"),
    'frozen':      (None, "--frozen",      "Show only frozen addresses"),
    'unused':      (None, "--unused",      "Show only unused addresses"),
    'funded':      (None, "--funded",      "Show only funded addresses"),
    'show_balance':("-b", "--balance",     "Show the balances of listed addresses"),
    'show_labels': ("-l", "--labels",      "Show the labels of listed addresses"),
    'nocheck':     (None, "--nocheck",     "Do not verify aliases"),
    'tx_fee':      ("-f", "--fee",         "Transaction fee (in BTC)"),
    'from_addr':   ("-F", "--from",        "Source address. If it isn't in the wallet, it will ask for the private key unless supplied in the format public_key:private_key. It's not saved in the wallet."),
    'change_addr': ("-c", "--change",      "Change address. Default is a spare address, or the source address if it's not in the wallet"),
    'nbits':       (None, "--nbits",       "Number of bits of entropy"),
    'entropy':     (None, "--entropy",     "Custom entropy"),
    'language':    ("-L", "--lang",        "Default language for wordlist"),
    'gap_limit':   ("-G", "--gap",         "Gap limit"),
    'privkey':     (None, "--privkey",     "Private key. Set to '?' to get a prompt."),
    'unsigned':    ("-u", "--unsigned",    "Do not sign transaction"),
    'domain':      ("-D", "--domain",      "List of addresses"),
    'account':     (None, "--account",     "Account"),
    'memo':        ("-m", "--memo",        "Description of the request"),
    'expiration':  (None, "--expiration",  "Time in seconds"),
    'force':       (None, "--force",       "Create new address beyong gap limit, if no more address is available."),
    'pending':     (None, "--pending",     "Show only pending requests."),
    'expired':     (None, "--expired",     "Show only expired requests."),
    'paid':        (None, "--paid",        "Show only paid requests."),
    'exclude_claimtrietx':(None,"--exclude_claimtrietx", "Exclude claimtrie transactions"),
    'return_addr': (None, "--return_addr", "Return address where amounts in abandoned claimtrie transactions are returned."),
    'claim_addr':  (None, "--claim_addr",  "Address where claims are sent."),
    'broadcast':   (None, "--broadcast",   "if True, broadcast the transaction")
}


# don't use floats because of rounding errors
json_loads = lambda x: json.loads(x, parse_float=lambda x: str(Decimal(x)))
arg_types = {
    'num': int,
    'nbits': int,
    'entropy': long,
    'tx': json_loads,
    'pubkeys': json_loads,
    'inputs': json_loads,
    'outputs': json_loads,
    'tx_fee': lambda x: str(Decimal(x)) if x is not None else None,
    'amount': lambda x: str(Decimal(x)) if x!='!' else '!',
}

config_variables = {

    'addrequest': {
        'requests_dir': 'directory where a bip70 file will be written.',
        'ssl_privkey': 'Path to your SSL private key, needed to sign the request.',
        'ssl_chain': 'Chain of SSL certificates, needed for signed requests. Put your certificate at the top and the root CA at the end',
        'url_rewrite': 'Parameters passed to str.replace(), in order to create the r= part of bitcoin: URIs. Example: \"(\'file:///var/www/\',\'https://lbryum.org/\')\"',
    },
    'listrequests':{
        'url_rewrite': 'Parameters passed to str.replace(), in order to create the r= part of bitcoin: URIs. Example: \"(\'file:///var/www/\',\'https://lbryum.org/\')\"',
    }
}

def set_default_subparser(self, name, args=None):
    """see http://stackoverflow.com/questions/5176691/argparse-how-to-specify-a-default-subcommand"""
    subparser_found = False
    for arg in sys.argv[1:]:
        if arg in ['-h', '--help']:  # global help if no subparser
            break
    else:
        for x in self._subparsers._actions:
            if not isinstance(x, argparse._SubParsersAction):
                continue
            for sp_name in x._name_parser_map.keys():
                if sp_name in sys.argv[1:]:
                    subparser_found = True
        if not subparser_found:
            # insert default in first position, this implies no
            # global options without a sub_parsers specified
            if args is None:
                sys.argv.insert(1, name)
            else:
                args.insert(0, name)

argparse.ArgumentParser.set_default_subparser = set_default_subparser



def add_network_options(parser):
    parser.add_argument("-1", "--oneserver", action="store_true", dest="oneserver", default=False, help="connect to one server only")
    parser.add_argument("-s", "--server", dest="server", default=None, help="set server host:port:protocol, where protocol is either t (tcp) or s (ssl)")
    parser.add_argument("-p", "--proxy", dest="proxy", default=None, help="set proxy [type:]host[:port], where type is socks4,socks5 or http")

from util import profiler

@profiler
def get_parser():
    # parent parser, because set_default_subparser removes global options
    parent_parser = argparse.ArgumentParser('parent', add_help=False)
    group = parent_parser.add_argument_group('global options')
    group.add_argument("-v", "--verbose", action="store_true", dest="verbose", default=False, help="Show debugging information")
    group.add_argument("-P", "--portable", action="store_true", dest="portable", default=False, help="Use local 'electrum_data' directory")
    group.add_argument("-w", "--wallet", dest="wallet_path", help="wallet path")
    # create main parser
    parser = argparse.ArgumentParser(
        parents=[parent_parser],
        epilog="Run 'lbryum help <command>' to see the help for a command")
    subparsers = parser.add_subparsers(dest='cmd', metavar='<command>')
    # gui
    parser_gui = subparsers.add_parser('gui', parents=[parent_parser], description="Run Electrum's Graphical User Interface.", help="Run GUI (default)")
    parser_gui.add_argument("url", nargs='?', default=None, help="bitcoin URI (or bip70 file)")
    #parser_gui.set_defaults(func=run_gui)
    parser_gui.add_argument("-g", "--gui", dest="gui", help="select graphical user interface", choices=['qt', 'kivy', 'text', 'stdio'])
    parser_gui.add_argument("-o", "--offline", action="store_true", dest="offline", default=False, help="Run offline")
    parser_gui.add_argument("-m", action="store_true", dest="hide_gui", default=False, help="hide GUI on startup")
    parser_gui.add_argument("-L", "--lang", dest="language", default=None, help="default language used in GUI")
    add_network_options(parser_gui)
    # daemon
    parser_daemon = subparsers.add_parser('daemon', parents=[parent_parser], help="Run Daemon")
    parser_daemon.add_argument("subcommand", choices=['start', 'status', 'stop'])
    #parser_daemon.set_defaults(func=run_daemon)
    add_network_options(parser_daemon)
    # commands
    for cmdname in sorted(known_commands.keys()):
        cmd = known_commands[cmdname]
        p = subparsers.add_parser(cmdname, parents=[parent_parser], help=cmd.help, description=cmd.description)
        #p.set_defaults(func=run_cmdline)
        if cmd.requires_password:
            p.add_argument("-W", "--password", dest="password", default=None, help="password")
        for optname, default in zip(cmd.options, cmd.defaults):
            a, b, help = command_options[optname]
            action = "store_true" if type(default) is bool else 'store'
            args = (a, b) if a else (b,)
            if action == 'store':
                _type = arg_types.get(optname, str)
                p.add_argument(*args, dest=optname, action=action, default=default, help=help, type=_type)
            else:
                p.add_argument(*args, dest=optname, action=action, default=default, help=help)

        for param in cmd.params:
            h = param_descriptions.get(param, '')
            _type = arg_types.get(param, str)
            p.add_argument(param, help=h, type=_type)

        cvh = config_variables.get(cmdname)
        if cvh:
            group = p.add_argument_group('configuration variables', '(set with setconfig/getconfig)')
            for k, v in cvh.items():
                group.add_argument(k, nargs='?', help=v)

    # 'gui' is the default command
    parser.set_default_subparser('gui')
    return parser
