#!/usr/bin/env python3
import sys
import subprocess
import re
import os
from email.utils import parseaddr
import sh
import logging
import argparse
from junitparser import TestCase, TestSuite, JUnitXml, Skipped, Error, Failure, Attr
from github import Github
from shutil import copyfile, copytree
import json
from pprint import  pprint

repository_path = os.getcwd()
if "ZEPHYR_BASE" not in os.environ:
    logging.warn("$ZEPHYR_BASE environment variable undefined.\n")
    zephyr_path = repository_path
else:
    zephyr_path = os.environ['ZEPHYR_BASE']

logger = None
DOCS_WARNING_FILE = "doc.warnings"


sh_special_args = {
    '_tty_out': False,
    '_cwd': repository_path
}

# Put the Kconfiglib path first to make sure no local Kconfiglib version is
# used
sys.path.insert(0, os.path.join(zephyr_path, "scripts/kconfig"))
import kconfiglib


def get_shas(refspec):

    sha_list = sh.git("rev-list",
        '--max-count={0}'.format(-1 if "." in refspec else 1),
        refspec, **sh_special_args).split()

    return sha_list


class MyCase(TestCase):
    classname = Attr()
    doc = Attr()


class ComplianceTest:

    _name = ""
    _title = ""
    _doc = "https://docs.zephyrproject.org/latest/contribute/contribute_guidelines.html"

    def __init__(self, suite, range):
        self.case = None
        self.suite = suite
        self.commit_range = range

    def prepare(self):
        self.case = MyCase(self._name)
        self.case.classname = "Guidelines"
        print("Running {} tests...".format(self._name))

    def run(self):
        pass


class CheckPatch(ComplianceTest):
    _name = "checkpatch"
    _doc = "https://docs.zephyrproject.org/latest/contribute/contribute_guidelines.html#coding-style"

    def run(self):
        self.prepare()
        diff = subprocess.Popen(('git', 'diff', '%s' %(self.commit_range)), stdout=subprocess.PIPE)
        try:
            output = subprocess.check_output(('%s/scripts/checkpatch.pl' %zephyr_path,
                '--mailback', '--no-tree', '-'), stdin=diff.stdout,
                stderr=subprocess.STDOUT, shell=True)

        except subprocess.CalledProcessError as ex:
            m = re.search("([1-9][0-9]*) errors,", ex.output.decode('utf8'))
            if m:
                self.case.result = Failure("Checkpatch issues", "failure")
                self.case.result._elem.text = (ex.output.decode('utf8'))


class KconfigCheck(ComplianceTest):
    _name = "Kconfig"
    _doc = "https://docs.zephyrproject.org/latest/application/kconfig-tips.html"

    def run(self):
        self.prepare()

        # Look up Kconfig files relative to ZEPHYR_BASE
        os.environ["srctree"] = zephyr_path

        # Parse the entire Kconfig tree, to make sure we see all symbols
        os.environ["SOC_DIR"] = "soc/"
        os.environ["BOARD_DIR"] = "boards/*/*"
        os.environ["ARCH"] = "*"

        # Enable strict Kconfig mode in Kconfiglib, which assumes there's just a
        # single Kconfig tree and warns for all references to undefined symbols
        os.environ["KCONFIG_STRICT"] = "y"

        undef_ref_warnings = []

        for warning in kconfiglib.Kconfig().warnings:
            if "undefined symbol" in warning:
                undef_ref_warnings.append(warning)

        # Generating multiple JUnit <failure>s would be neater, but Shippable only
        # seems to display the first one
        if undef_ref_warnings:
            self.case.result = Failure("undefined Kconfig symbols", "failure")
            self.case.result._elem.text = "\n\n\n".join(undef_ref_warnings)


class Documentation(ComplianceTest):
    _name = "Documentation"
    _doc = "https://docs.zephyrproject.org/latest/contribute/doc-guidelines.html"

    def run(self):
        self.prepare()

        if os.path.exists(DOCS_WARNING_FILE) and os.path.getsize(DOCS_WARNING_FILE) > 0:
            with open(DOCS_WARNING_FILE, "rb") as f:
                log = f.read()

                self.case.result = Error("Documentation Issues", "failure")
                self.case.result._elem.text = log.decode('utf8')


class GitLint(ComplianceTest):
    _name = "Gitlint"
    _doc = "https://docs.zephyrproject.org/latest/contribute/contribute_guidelines.html#commit-guidelines"

    def run(self):
        self.prepare()

        proc = subprocess.Popen('gitlint --commits %s' % (self.commit_range),
                                shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        msg = ""
        if proc.wait() != 0:
            msg = proc.stdout.read()

        if msg != "":
            text = (msg.decode('utf8'))
            self.case.result = Failure("commit message syntax issues", "failure")
            self.case.result._elem.text = text



class License(ComplianceTest):
    _name = "License"
    _doc  = "https://docs.zephyrproject.org/latest/contribute/contribute_guidelines.html#licensing"

    def run(self):
        self.prepare()

        scancode = "/opt/scancode-toolkit/scancode"
        if not os.path.exists(scancode):
            self.case.result = Skipped("scancode-toolkit not installed", "skipped")
            return

        os.makedirs("scancode-files", exist_ok=True)
        new_files = sh.git("diff", "--name-only", "--diff-filter=A", self.commit_range, **sh_special_args)

        if len(new_files) == 0:
            return

        for newf in new_files:
            f = str(newf).rstrip()
            os.makedirs(os.path.join('scancode-files', os.path.dirname(f)), exist_ok=True)
            copy = os.path.join("scancode-files", f)
            copyfile(f, copy)

        try:
            cmd = [scancode, '--verbose', '--copyright', '--license', '--license-diag', '--info',
                    '--classify', '--summary', '--json', 'scancode.json', 'scancode-files/']

            cmd_str = " ".join(cmd)
            logging.info(cmd_str)

            out = subprocess.check_output(cmd_str, stderr=subprocess.STDOUT, shell=True)

        except subprocess.CalledProcessError as e:
            logging.error(e.output)
            self.case.result = Skipped("Exception when running scancode", "skipped")
            return

        report = ""
        with open ('scancode.json', 'r') as json_fp:
            scancode_results = json.load(json_fp)
            for file in scancode_results['files']:
                if file['type'] == 'directory':
                    continue

                original_fp = str(file['path']).replace('scancode-files/', '')
                licenses = file['licenses']
                if (file['is_script'] or file['is_source']) and (file['programming_language'] not in ['CMake']) and (file['extension'] not in ['.yaml']):
                    if len(file['licenses']) == 0:
                        report += ("* {} missing license.\n".format(original_fp))
                    else:
                        for l in licenses:
                            if l['key'] != "apache-2.0":
                                report += ("* {} is not apache-2.0 licensed: {}\n".format(original_fp, l['key']))
                            if l['category'] != 'Permissive':
                                report += ("* {} has non-permissive license: {}\n".format(original_fp, l['key']))

                    if len(file['copyrights'])  == 0:
                        report += ("* {} missing copyright.\n".format(original_fp))

        if report != "":
            self.case.result = Failure("License/Copyright issues", "failure")
            self.case.result._elem.text = report




class Identity(ComplianceTest):
    _name = "Identity/Emails"
    _doc = "https://docs.zephyrproject.org/latest/contribute/contribute_guidelines.html#commit-guidelines"

    def run(self):
        self.prepare()

        for f in get_shas(self.commit_range):
            commit = sh.git("log", "--decorate=short", "-n 1", f, **sh_special_args)
            signed = []
            author = ""
            sha = ""
            parsed_addr = None
            for line in commit.split("\n"):
                match = re.search("^commit\s([^\s]*)", line)
                if match:
                    sha = match.group(1)
                match = re.search("^Author:\s(.*)", line)
                if match:
                    author = match.group(1)
                    parsed_addr = parseaddr(author)
                match = re.search("signed-off-by:\s(.*)", line, re.IGNORECASE)
                if match:
                    signed.append(match.group(1))

            error1 = "%s: author email (%s) needs to match one of the signed-off-by entries." % (sha, author)
            error2 = "%s: author email (%s) does not follow the syntax: First Last <email>." % (sha, author)
            failure = None
            if author not in signed:
                failure = error1

            if not parsed_addr or len(parsed_addr[0].split(" ")) < 2:
                if not failure:

                    failure = error2
                else:
                    failure = failure + "\n" + error2


            if failure:
                self.case.result = Failure("identity/email issues", "failure")
                self.case.result._elem.text = failure


def init_logs():
    global logger
    log_lev = os.environ.get('LOG_LEVEL', None)
    level = logging.INFO
    if log_lev == "DEBUG":
        level = logging.DEBUG
    elif log_lev == "ERROR":
        level = logging.ERROR

    console = logging.StreamHandler()
    format = logging.Formatter('%(levelname)-8s: %(message)s')
    console.setFormatter(format)
    logger = logging.getLogger('')
    logger.addHandler(console)
    logger.setLevel(level)

    logging.debug("Log init completed")

def parse_args():
    parser = argparse.ArgumentParser(
                description="Check for coding style and documentation warnings.")
    parser.add_argument('-c', '--commits', default=None,
            help="Commit range in the form: a..b")
    parser.add_argument('-g', '--github', action="store_true",
                        help="Send results to github as a comment.")

    parser.add_argument('-r', '--repo', default=None,
                        help="Github repository")
    parser.add_argument('-p', '--pull-request', default=0, type=int,
                        help="Pull request number")

    parser.add_argument('-s', '--status', action="store_true", help="Set status to pending")
    parser.add_argument('-S', '--sha', default=None, help="Commit SHA")
    return parser.parse_args()



def set_status(gh, repo, sha):

    repo = gh.get_repo(repo)
    commit = repo.get_commit(sha)
    for Test in ComplianceTest.__subclasses__():
        t = Test(None, "")
        print("Creating status for %s" %(t._name))
        commit.create_status('pending',
                             '%s' %t._doc,
                             'Verification in progress',
                             '{}'.format(t._name))


def main():
    args = parse_args()


    github_token = ''
    gh = None
    if args.github:
        github_token = os.environ['GH_TOKEN']
        gh = Github(github_token)

    if args.status and args.sha != None and args.repo and gh:
        set_status(gh, args.repo, args.sha)
        sys.exit(0)

    if not args.commits:
        sys.exit(1)

    suite = TestSuite("Compliance")
    docs = {}
    for Test in ComplianceTest.__subclasses__():
        t = Test(suite, args.commits)
        t.run()
        suite.add_testcase(t.case)
        docs[t.case.name] = t._doc

    xml = JUnitXml()
    xml.add_testsuite(suite)
    xml.update_statistics()
    xml.write('compliance.xml')

    if args.github:
        repo = gh.get_repo(args.repo)
        pr = repo.get_pull(int(args.pull_request))
        commit = repo.get_commit(args.sha)

        comment = "Found the following issues, please fix and resubmit:\n\n"
        comment_count = 0
        print("Processing results...")
        for case in suite:
            if case.result and case.result.type != 'skipped':
                comment_count += 1
                comment += ("## {}\n".format(case.result.message))
                comment += "\n"
                if case.name not in ['Gitlint', 'Identity/Emails', 'License']:
                    comment += "```\n"
                comment += ("{}\n".format(case.result._elem.text))
                if case.name not in ['Gitlint', 'Identity/Emails', 'License']:
                    comment += "```\n"

                commit.create_status('failure',
                                     docs[case.name],
                                     'Verification failed',
                                     '{}'.format(case.name))
            else:
                commit.create_status('success',
                                     docs[case.name],
                                     'Verifications passed',
                                     '{}'.format(case.name))

        if args.repo and args.pull_request and comment_count > 0:
            comments = pr.get_issue_comments()
            commented = False
            for c in comments:
                if 'Found the following issues, please fix and resubmit' in c.body:
                    c.edit(comment)
                    commented = True
                    break

            if not commented:
                pr.create_issue_comment(comment)


if __name__ == "__main__":
    main()

