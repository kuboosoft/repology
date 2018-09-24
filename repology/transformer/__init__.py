# Copyright (C) 2016-2018 Dmitry Marakasov <amdmi3@amdmi3.ru>
#
# This file is part of repology
#
# repology is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# repology is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with repology.  If not, see <http://www.gnu.org/licenses/>.

import os
import pprint
import re

import yaml

from repology.transformer.blocks import CoveringRuleBlock, NameMapRuleBlock, SingleRuleBlock
from repology.transformer.rule import PackageContext, Rule, RuleApplyResult


RULE_LOWFREQ_THRESHOLD = 0.001  # best of 0.1, 0.01, 0.001, 0.0001
COVERING_BLOCK_MIN_SIZE = 2  # covering block over single block impose extra overhead
NAMEMAP_BLOCK_MIN_SIZE = 1  # XXX: test > 1 after rule optimizations


class PackageTransformer:
    def __init__(self, repomgr, rulesdir=None, rulestext=None):
        self.repomgr = repomgr

        self.rules = []

        if rulestext:
            self.rules = yaml.safe_load(rulestext)
        else:
            rulefiles = []

            for root, dirs, files in os.walk(rulesdir):
                rulefiles += [os.path.join(root, f) for f in files if f.endswith('.yaml')]
                dirs[:] = [d for d in dirs if not d.startswith('.')]

            for rulefile in sorted(rulefiles):
                with open(rulefile) as data:
                    self.rules += yaml.safe_load(data)

        pp = pprint.PrettyPrinter(width=10000)
        for rulenum, rule in enumerate(self.rules):
            # save pretty-print before all transformations
            rule['pretty'] = pp.pformat(rule)

            # convert some fields to lists
            for field in ['name', 'ver', 'category', 'family', 'ruleset', 'noruleset', 'wwwpart', 'flag', 'noflag', 'addflag']:
                if field in rule and not isinstance(rule[field], list):
                    rule[field] = [rule[field]]

            # support legacy
            if 'family' in rule and 'ruleset' in rule:
                raise RuntimeError('both ruleset and family in rule!')
            elif 'family' in rule and 'ruleset' not in rule:
                rule['ruleset'] = rule.pop('family')

            # convert some fields to sets
            for field in ['ruleset', 'noruleset', 'flag', 'noflag']:
                if field in rule:
                    rule[field] = set(rule[field])

            # convert some fields to lowercase
            for field in ['category', 'wwwpart']:
                if field in rule:
                    rule[field] = [s.lower() for s in rule[field]]

            # compile regexps (replace here handles multiline regexps)
            for field in ['namepat', 'wwwpat']:
                if field in rule:
                    rule[field] = re.compile(rule[field].replace('\n', ''), re.ASCII)

            for field in ['verpat']:
                if field in rule:  # verpat is case insensitive
                    rule[field] = re.compile(rule[field].lower().replace('\n', ''), re.ASCII)

            rule['matches'] = 0
            rule['number'] = rulenum

        self.rules = [Rule(ruledata) for ruledata in self.rules]

        self.ruleblocks = []

        current_name_rules = []

        def flush_current_name_rules():
            nonlocal current_name_rules
            if len(current_name_rules) >= NAMEMAP_BLOCK_MIN_SIZE:
                self.ruleblocks.append(NameMapRuleBlock(current_name_rules))
            elif current_name_rules:
                self.ruleblocks.extend([SingleRuleBlock(rule) for rule in current_name_rules])
            current_name_rules = []

        for rule in self.rules:
            if 'name' in rule:
                current_name_rules.append(rule)
            else:
                flush_current_name_rules()
                self.ruleblocks.append(SingleRuleBlock(rule))

        flush_current_name_rules()

        self.optruleblocks = self.ruleblocks
        self.packages_processed = 0

    def _recalc_opt_ruleblocks(self):
        self.optruleblocks = []

        current_lowfreq_blocks = []

        def flush_current_lowfreq_blocks():
            nonlocal current_lowfreq_blocks
            if len(current_lowfreq_blocks) >= COVERING_BLOCK_MIN_SIZE:
                self.optruleblocks.append(CoveringRuleBlock(current_lowfreq_blocks))
            elif current_lowfreq_blocks:
                self.optruleblocks.extend(current_lowfreq_blocks)
            current_lowfreq_blocks = []

        for block in self.ruleblocks:
            max_matches = 0
            has_unconditional = False
            for rule in block.iter_all_rules():
                max_matches = max(max_matches, rule['matches'])
                if 'name' not in rule and 'namepat' not in rule:
                    has_unconditional = True
                    break

            if has_unconditional or max_matches >= self.packages_processed * RULE_LOWFREQ_THRESHOLD:
                flush_current_lowfreq_blocks()
                self.optruleblocks.append(block)
                continue

            current_lowfreq_blocks.append(block)

        flush_current_lowfreq_blocks()

    def _iter_package_rules(self, package):
        for ruleblock in self.optruleblocks:
            yield from ruleblock.iter_rules(package)

    def Process(self, package):
        self.packages_processed += 1

        if self.packages_processed == 1000 or self.packages_processed == 10000 or self.packages_processed == 100000 or self.packages_processed == 1000000:
            self._recalc_opt_ruleblocks()

        # start with package.name as is, if it was not already set
        if package.effname is None:
            package.effname = package.name

        package_context = PackageContext(self.repomgr.GetRepository(package.repo)['ruleset'])

        for rule in self._iter_package_rules(package):
            match_context = rule.match(package, package_context)
            if match_context:
                if rule.apply(package, package_context, match_context) == RuleApplyResult.last:
                    return

    def GetUnmatchedRules(self):
        result = []

        for rule in self.rules:
            if rule['matches'] == 0:
                result.append(rule['pretty'])

        return result
