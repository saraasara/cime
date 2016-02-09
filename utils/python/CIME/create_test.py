"""
Implementation of create_test functionality from CIME
"""
import shutil, traceback, stat, glob, threading, time, thread
from XML.standard_module_setup import *
from copy import deepcopy
import compare_namelists
import CIME.utils
from CIME.utils import expect, run_cmd
import wait_for_tests, update_acme_tests
from wait_for_tests import TEST_PASS_STATUS, TEST_FAIL_STATUS, TEST_PENDING_STATUS, TEST_STATUS_FILENAME, NAMELIST_FAIL_STATUS, RUN_PHASE, NAMELIST_PHASE
from CIME.XML.machines import Machines
from CIME.XML.env_test import EnvTest
from CIME.XML.files import Files
from CIME.XML.component import Component
from CIME.XML.testlist import Testlist
from CIME.XML.testspec import TestSpec

INITIAL_PHASE = "INIT"
CREATE_NEWCASE_PHASE = "CREATE_NEWCASE"
XML_PHASE   = "XML"
SETUP_PHASE = "SETUP"
BUILD_PHASE = "BUILD"
TEST_STATUS_PHASE = "TEST_STATUS"
PHASES = [INITIAL_PHASE, CREATE_NEWCASE_PHASE, XML_PHASE, SETUP_PHASE, NAMELIST_PHASE, BUILD_PHASE, RUN_PHASE] # Order matters
CONTINUE = [TEST_PASS_STATUS, NAMELIST_FAIL_STATUS]

###############################################################################
class CreateTest(object):
###############################################################################

    ###########################################################################
    def __init__(self, test_names,
                 no_run=False, no_build=False, no_batch=None,
                 test_root=None, test_id=None,
                 machine_name=None,compiler=None,
                 baseline_root=None, baseline_name=None,
                 clean=False,compare=False, generate=False, namelists_only=False,
                 project=None, parallel_jobs=None,
                 xml_machine=None, xml_compiler=None, xml_category=None,xml_testlist=None):
    ###########################################################################
        self._cime_root = CIME.utils.get_cime_root()
        self._cime_model = CIME.utils.get_model()
        # needed for perl interface
        os.environ["CIMEROOT"] = self._cime_root
        self._machobj   = Machines(machine=machine_name)
        machine_name    = self._machobj.get_machine_name()

        self._no_build = no_build      if not namelists_only else True
        self._no_run   = no_run        if not self._no_build else True

        # Figure out what project to use
        if (project is None):
            self._project = CIME.utils.get_project()
            if (self._project is None):
                self._project = self._machobj.get_value("PROJECT")
        else:
            self._project = project

        # We will not use batch system if user asked for no_batch or if current
        # machine is not a batch machine
        self._no_batch = no_batch or not self._machobj.has_batch_system()

        self._test_root = test_root if test_root is not None else self._machobj.get_value("CESMSCRATCHROOT")
        if (self._project is not None):
            self._test_root = self._test_root.replace("$PROJECT", self._project)
        self._test_root = os.path.abspath(self._test_root)

        self._test_id = test_id if test_id is not None else CIME.utils.get_utc_timestamp()

        self._compiler = compiler if compiler is not None else self._machobj.get_default_compiler()
        expect(self._machobj.is_valid_compiler(self._compiler),
               "Compiler %s not valid for machine %s" % (self._compiler,machine_name))

        self._clean          = clean
        self._compare        = compare
        self._generate       = generate
        self._namelists_only = namelists_only

        # If xml options are provided get tests from xml file, otherwise use acme dictionary
        if(not test_names and (xml_machine is not None or xml_category is not None or xml_compiler is not None or xml_testlist is not None)):
            self._tests = self._get_tests_from_xml(xml_machine,xml_category,xml_compiler, xml_testlist,machine_name ,compiler)
        else:
            expect(len(test_names) > 0, "No tests to run")
            test_names = update_acme_tests.get_full_test_names(test_names, machine_name, self._compiler)
            self._tests  = self._convert_testlist_to_dict(test_names)

        if (parallel_jobs is None):
            self._parallel_jobs  = min(len(self._tests), int(self._machobj.get_value("MAX_TASKS_PER_NODE")))
        else:
            self._parallel_jobs = parallel_jobs

        if (self._compare or self._generate):

            # Figure out what baseline name to use
            if (baseline_name is None):
                branch_name = CIME.utils.get_current_branch(repo=self._cime_root)
                expect(branch_name is not None, "Could not determine baseline name from branch, please use -b option")
                self._baseline_name = os.path.join(self._compiler, branch_name)
            else:
                self._baseline_name  = baseline_name
                if (not self._baseline_name.startswith("%s/" % self._compiler)):
                    self._baseline_name = os.path.join(self._compiler, self._baseline_name)

            # Compute baseline_root
            self._baseline_root = baseline_root if baseline_root is not None else self._machobj.get_value("CCSM_BASELINE")
            if (self._project is not None):
                self._baseline_root = self._baseline_root.replace("$PROJECT", self._project)
            self._baseline_root = os.path.abspath(self._baseline_root)

            if (self._compare):
                full_baseline_dir = os.path.join(self._baseline_root, self._baseline_name)
                expect(os.path.isdir(full_baseline_dir),
                       "Missing baseline comparison directory %s" % full_baseline_dir)
        else:
            self._baseline_root = None
        # This is the only data that multiple threads will simultaneously access
        # Each test has it's own index and setting/retrieving items from a list
        # is atomic, so this should be fine to use without mutex
        for test in self._tests:
            test["phase"] = INITIAL_PHASE
            test["status"] = TEST_PASS_STATUS

        # Oversubscribe by 1/4
        pes = int(self._machobj.get_value("PES_PER_NODE"))
        self._proc_pool = int(pes * 1.25)

        # Since the name-list phase can fail without aborting later phases, we
        # need some extra state to remember tests that had namelist problems
        self._tests_with_nl_problems = [None] * len(self._tests)

        # Setup phases
        self._phases = list(PHASES)
        if (no_build):
            self._phases.remove(BUILD_PHASE)
        if (no_run):
            self._phases.remove(RUN_PHASE)
        if (not self._compare and not self._generate):
            self._phases.remove(NAMELIST_PHASE)

        # None of the test directories should already exist.
        for test in self._tests:
            expect(not os.path.exists(self._get_test_dir(test["name"])),
                   "Cannot create new case in directory '%s', it already exists. Pick a different test-id" % self._get_test_dir(test["name"]))
        if(self._cime_model == "cesm"):
            self._testspec = TestSpec(os.path.join(self._test_root,"testspec_%s.xml" % self._test_id))
            self._testspec.set_header(self._test_root, machine_name,self._test_id,baselineroot=self._baseline_root)


        # By the end of this constructor, this program should never hard abort,
        # instead, errors will be placed in the TestStatus files for the various
        # tests cases

    ###########################################################################
    def _log_output(self, test_name, output):
    ###########################################################################
        test_dir = self._get_test_dir(test_name)
        if (not os.path.isdir(test_dir)):
            # Note: making this directory could cause create_newcase to fail
            # if this is run before.
            os.makedirs(test_dir)

        with open(os.path.join(test_dir, "TestStatus.log"), "a") as fd:
            fd.write(output)

    ###########################################################################
    def _get_case_id(self, test_name):
    ###########################################################################
        baseline_action_code = ".C" if self._compare else (".G" if self._generate else "")
        return "%s%s.%s" % (test_name, baseline_action_code, self._test_id)

    ###########################################################################
    def _get_test_dir(self, test_name):
    ###########################################################################
        return os.path.join(self._test_root, self._get_case_id(test_name))

    ###########################################################################
    def _get_test_data(self, test):
    ###########################################################################
        return (test["phase"],test["status"])

    ###########################################################################
    def _is_broken(self, test):
    ###########################################################################
        status = self._get_test_status(test)
        return status not in CONTINUE and status != TEST_PENDING_STATUS

    ###########################################################################
    def _work_remains(self, test):
    ###########################################################################
        test_phase, test_status = self._get_test_data(test)
        return (test_status in CONTINUE or test_status == TEST_PENDING_STATUS) and test_phase != self._phases[-1]

    ###########################################################################
    def _get_test_status(self, test, phase=None):
    ###########################################################################
        curr_phase = self._get_test_phase(test)
        if (phase == NAMELIST_PHASE and test["name"] in self._tests_with_nl_problems):
            return NAMELIST_FAIL_STATUS
        elif (phase is None or phase == curr_phase):
            return self._get_test_data(test)[1]
        else:
            expect(phase is None or self._phases.index(phase) < self._phases.index(curr_phase),
                   "Tried to see the future")
            # Assume all older phases PASSed
            return TEST_PASS_STATUS

    ###########################################################################
    def _get_test_phase(self, test):
    ###########################################################################
        return self._get_test_data(test)[0]

    ###########################################################################
    def _update_test_status(self, test, phase, status):
    ###########################################################################
        phase_idx = self._phases.index(phase)
        old_phase, old_status = self._get_test_data(test)

        if (old_phase == phase):
            expect(old_status == TEST_PENDING_STATUS,
                   "Only valid to transition from PENDING to something else, found '%s'" % old_status)
            expect(status != TEST_PENDING_STATUS,
                   "Cannot transition from PEND -> PEND")
        else:
            expect(old_status in CONTINUE,
                   "Why did we move on to next phase when prior phase did not pass?")
            expect(status == TEST_PENDING_STATUS,
                   "New phase should be set to pending status")
            expect(self._phases.index(old_phase) == phase_idx - 1,
                   "Skipped phase?")
        test["phase"] = phase
        test["status"] = status
        if(self._cime_model == "cesm"):
            self._testspec.update_test_status(test["name"],phase,status)
    ###########################################################################
    def _run_phase_command(self, test, cmd, phase, from_dir=None):
    ###########################################################################
        test_name = test["name"]
        while (True):
            rc, output, errput = run_cmd(cmd, ok_to_fail=True, from_dir=from_dir)
            if (rc != 0):
                self._log_output(test_name,
                                 "%s FAILED for test '%s'.\nCommand: %s\nOutput: %s\n\nErrput: %s" %
                                 (phase, test_name, cmd, output, errput))
                # Temporary hack to get around odd file descriptor use by
                # buildnml scripts.
                if ("bad interpreter" in errput):
                    time.sleep(1)
                    continue
                else:
                    break
            else:
                self._log_output(test_name,
                                 "%s PASSED for test '%s'.\nCommand: %s\nOutput: %s\n\nErrput: %s" %
                                 (phase, test_name, cmd, output, errput))
                break

        return rc == 0

    ###########################################################################
    def _create_newcase_phase(self, test):
    ###########################################################################
        test_name = test["name"]
        test_dir = self._get_test_dir(test_name)

        test_case, case_opts, grid, compset, machine, compiler, test_mods = CIME.utils.parse_test_name(test_name)
        if (compiler != self._compiler):
            raise StandardError("Test '%s' has compiler that does not match instance compliler '%s'" % (test_name, self._compiler))
        if (self._parallel_jobs == 1):
            scratch_dir = self._machobj.get_value("CESMSCRATCHROOT")
            if (self._project is not None):
                scratch_dir = scratch_dir.replace("$PROJECT", self._project)
            sharedlibroot = os.path.join(scratch_dir, "sharedlibroot.%s" % self._test_id)
        else:
            # Parallelizing builds introduces potential sync problems with sharedlibroot
            # Just let every case build it's own
            sharedlibroot = os.path.join(test_dir, "sharedlibroot.%s" % self._test_id)
        create_newcase_cmd = "%s -model %s -case %s -res %s -mach %s -compiler %s -compset %s -testname %s -project %s -nosavetiming -sharedlibroot %s" % \
                              (os.path.join(self._cime_root,"scripts", "create_newcase"),
                               self._cime_model,test_dir, grid, machine, compiler, compset, test_case, self._project,
                               sharedlibroot)
        if (case_opts is not None):
            create_newcase_cmd += " -confopts _%s" % ("_".join(case_opts))
        if (test_mods is not None):
            test_mod_file = os.path.join(self._cime_root, "scripts", "Testing", "Testlistxml", "testmods_dirs", test_mods)
            if (not os.path.exists(test_mod_file)):
                self._log_output(test_name, "Missing testmod file '%s'" % test_mod_file)
                return False
            create_newcase_cmd += " -user_mods_dir %s" % test_mod_file
        logging.info("Calling create_newcase: "+create_newcase_cmd)
        return self._run_phase_command(test, create_newcase_cmd, CREATE_NEWCASE_PHASE)

    ###########################################################################
    def _xml_phase(self, test):
    ###########################################################################
        test_name = test["name"]
        test_case = CIME.utils.parse_test_name(test_name)[0]
        xml_file = os.path.join(self._get_test_dir(test_name), "env_test.xml")
        envtest = EnvTest()

        files = Files()
        drv_config_file = files.get_value("CONFIG_DRV_FILE")
        logging.info("Found drv_config_file %s" % drv_config_file)

        drv_comp = Component(drv_config_file)
        envtest.add_elements_by_group(drv_comp, {}   ,'env_test.xml')
        envtest.set_value("TESTCASE",test_case)
        envtest.set_value("TEST_TESTID",self._test_id)
        envtest.set_value("CASEBASEID",test_name)

        test_argv = "-testname %s -testroot %s" % (test_name, self._test_root)
        if (self._generate):
            test_argv += " -generate %s" % self._baseline_name
            envtest.set_value("BASELINE_NAME_GEN",self._baseline_name)
            envtest.set_value("BASEGEN_CASE",os.path.join(self._baseline_name,test_name))
        if (self._compare):
            test_argv += " -compare %s" % self._baseline_name
            envtest.set_value("BASELINE_NAME_CMP",self._baseline_name)
            envtest.set_value("BASECMP_CASE",os.path.join(self._baseline_name,test_name))

        envtest.set_value("TEST_ARGV",test_argv)
        envtest.set_value("CLEANUP",("TRUE" if self._clean else "FALSE"))

        if (self._generate or self._compare):
            envtest.set_value("BASELINE_ROOT",self._baseline_root)

        envtest.set_value("GENERATE_BASELINE", "TRUE" if self._generate else "FALSE")
        envtest.set_value("COMPARE_BASELINE", "TRUE" if self._compare else "FALSE")
        envtest.set_value("CCSM_CPRNC", self._machobj.get_value("CCSM_CPRNC",resolved=False))
        envtest.write(xml_file)
        return True

    ###########################################################################
    def _setup_phase(self, test):
    ###########################################################################
        test_name = test["name"]
        test_case = CIME.utils.parse_test_name(test_name)[0]
        test_dir  = self._get_test_dir(test_name)
        test_case_definition_dir = os.path.join(self._cime_root, "scripts", "Testing", "Testcases")
        test_build = os.path.join(test_dir, "case.test_build" )

        if (os.path.exists(os.path.join(test_case_definition_dir, "%s_build.csh" % test_case))):
            shutil.copy(os.path.join(test_case_definition_dir, "%s_build.csh" % test_case), test_build)
        else:
            shutil.copy(os.path.join(test_case_definition_dir, "tests_build.csh"), test_build)

        return self._run_phase_command(test, "./case.setup", SETUP_PHASE, from_dir=test_dir)

    ###########################################################################
    def _nlcomp_phase(self, test):
    ###########################################################################
        test_name = test["name"]
        test_dir          = self._get_test_dir(test_name)
        casedoc_dir       = os.path.join(test_dir, "CaseDocs")
        baseline_dir      = os.path.join(self._baseline_root, self._baseline_name, test_name)
        baseline_casedocs = os.path.join(baseline_dir, "CaseDocs")
        compare_nl        = os.path.join(CIME.utils.get_acme_scripts_root(), "compare_namelists")
        simple_compare    = os.path.join(CIME.utils.get_acme_scripts_root(), "simple_compare")

        if (self._compare):
            has_fails = False

            # Start off by comparing everything in CaseDocs except a few arbitrary files (ugh!)
            # TODO: Namelist files should have consistent suffix
            all_items_to_compare = \
                [ item for item in glob.glob("%s/*" % casedoc_dir) if "README" not in os.path.basename(item) and not item.endswith("doc") and not item.endswith("prescribed") and not os.path.basename(item).startswith(".")] + \
                glob.glob("%s/*user_nl*" % test_dir)
            for item in all_items_to_compare:
                baseline_counterpart = os.path.join(baseline_casedocs if os.path.dirname(item).endswith("CaseDocs") else baseline_dir,
                                                    os.path.basename(item))
                if (not os.path.exists(baseline_counterpart)):
                    self._log_output(test_name, "Missing baseline namelist '%s'" % baseline_counterpart)
                    has_fails = True
                else:
                    if (compare_namelists.is_namelist_file(item)):
                        rc, output, _  = run_cmd("%s %s %s -c %s 2>&1" % (compare_nl, baseline_counterpart, item, test_name), ok_to_fail=True)
                    else:
                        rc, output, _  = run_cmd("%s %s %s -c %s 2>&1" % (simple_compare, baseline_counterpart, item, test_name), ok_to_fail=True)

                    if (rc != 0):
                        has_fails = True
                        self._log_output(test_name, output)

            if (has_fails):
                idx = self._tests.index(test_name)
                self._tests_with_nl_problems[idx] = test_name

        elif (self._generate):
            if (not os.path.isdir(baseline_dir)):
                os.makedirs(baseline_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IXOTH | stat.S_IROTH)

            if (os.path.isdir(baseline_casedocs)):
                shutil.rmtree(baseline_casedocs)
            shutil.copytree(casedoc_dir, baseline_casedocs)
            for item in glob.glob(os.path.join(test_dir, "user_nl*")):
                shutil.copy2(item, baseline_dir)

        # Always mark as passed unless we hit exception
        return True

    ###########################################################################
    def _build_phase(self, test):
    ###########################################################################
        test_name = test["name"]
        test_dir = self._get_test_dir(test_name)
        return self._run_phase_command(test, "./case.test_build", BUILD_PHASE, from_dir=test_dir)

    ###########################################################################
    def _run_phase(self, test):
    ###########################################################################
        test_dir = self._get_test_dir(test["name"])
        if ('wallclock' in test):
            out = run_cmd("./xmlchange JOB_WALLCLOCK_TIME=%s"%test["wallclock"], from_dir=test_dir)

        return self._run_phase_command(test, "./case.submit", RUN_PHASE, from_dir=test_dir)

    ###########################################################################
    def _update_test_status_file(self, test):
    ###########################################################################
        # TODO: The run scripts heavily use the TestStatus file. So we write out
        # the phases we have taken care of and then let the run scrips go from there
        # Eventually, it would be nice to have TestStatus management encapsulated
        # into a single place.
        test_name = test["name"]
        str_to_write = ""
        made_it_to_phase = self._get_test_phase(test)
        made_it_to_phase_idx = self._phases.index(made_it_to_phase)
        for phase in self._phases[0:made_it_to_phase_idx+1]:
            str_to_write += "%s %s %s\n" % (self._get_test_status(test, phase), test_name, phase)

        if (not self._no_run and not self._is_broken(test) and made_it_to_phase == BUILD_PHASE):
            # Ensure PEND state always gets added to TestStatus file if we are
            # about to run test
            str_to_write += "%s %s %s\n" % (TEST_PENDING_STATUS, test_name, RUN_PHASE)

        test_status_file = os.path.join(self._get_test_dir(test_name), TEST_STATUS_FILENAME)
        with open(test_status_file, "w") as fd:
            fd.write(str_to_write)

    ###########################################################################
    def _run_catch_exceptions(self, test, phase, run):
    ###########################################################################
        try:
            return run(test)
        except Exception as e:
            exc_tb = sys.exc_info()[2]
            errput = "Test '%s' failed in phase '%s' with exception '%s'" % (test["name"], phase, str(e))
            self._log_output(test["name"], errput)
            logging.warning("Caught exception: %s" % str(e))
            traceback.print_tb(exc_tb)
            return False

    ###########################################################################
    def _get_procs_needed(self, test_name, phase):
    ###########################################################################
        if (phase == RUN_PHASE and self._no_batch):
            test_dir = self._get_test_dir(test_name)
            out = run_cmd("./xmlquery TOTALPES -value", from_dir=test_dir)
            return int(out)
        else:
            return 1

    ###########################################################################
    def _handle_test_status_file(self, test, test_phase, success):
    ###########################################################################
        #
        # This complexity is due to sharing of TestStatus responsibilities
        #
        test_name = test["name"]
        try:
            if (test_phase != RUN_PHASE and
                (not success or test_phase == BUILD_PHASE or test_phase == self._phases[-1])):
                self._update_test_status_file(test)

            # If we failed VERY early on in the run phase, it's possible that
            # the CIME scripts never got a chance to set the state.
            elif (test_phase == RUN_PHASE and not success):
                test_status_file = os.path.join(self._get_test_dir(test_name), TEST_STATUS_FILENAME)
                statuses = wait_for_tests.parse_test_status_file(test_status_file)[0]
                if ( RUN_PHASE not in statuses or
                     statuses[RUN_PHASE] in [TEST_PASS_STATUS, TEST_PENDING_STATUS] ):
                    self._update_test_status_file(test)

        except Exception as e:
            # TODO: What to do here? This failure is very severe because the
            # only way for test results to be communicated is by the TestStatus
            # file.
            logging.critical("VERY BAD! Could not handle TestStatus file '%s': '%s'" %
                    (os.path.join(self._get_test_dir(test_name), TEST_STATUS_FILENAME), str(e)))
            thread.interrupt_main()

    ###########################################################################
    def _wait_for_something_to_finish(self, threads_in_flight):
    ###########################################################################
        expect(len(threads_in_flight) <= self._parallel_jobs, "Oversubscribed?")
        finished_tests = []
        while (not finished_tests):
            for test_name, thread_info in threads_in_flight.iteritems():
                if (not thread_info[0].is_alive()):
                    finished_tests.append( (test_name, thread_info[1]) )

            if (not finished_tests):
                time.sleep(0.2)

        for finished_test, procs_needed in finished_tests:
            self._proc_pool += procs_needed
            del threads_in_flight[finished_test]

    ###########################################################################
    def _consumer(self, test, test_phase, phase_method):
    ###########################################################################
        before_time = time.time()
        success = self._run_catch_exceptions(test, test_phase, phase_method)
        elapsed_time = time.time() - before_time
        status  = (TEST_PENDING_STATUS if test_phase == RUN_PHASE and not self._no_batch else TEST_PASS_STATUS) if success else TEST_FAIL_STATUS

        if (status != TEST_PENDING_STATUS):
            self._update_test_status(test, test_phase, status)
        self._handle_test_status_file(test, test_phase, success)

        status_str = "Finished %s for test %s in %f seconds (%s)\n" % (test_phase, test["name"], elapsed_time, status)
        if (not success):
            status_str += "    Case dir: %s\n" % self._get_test_dir(test["name"])
        sys.stdout.write(status_str)

    ###########################################################################
    def _producer(self):
    ###########################################################################
        threads_in_flight = {} # test-name -> (thread, procs)
        while (True):
            work_to_do = False
            num_threads_launched_this_iteration = 0
            for test in self._tests:
                if (type(test) == type(dict())):
                    test_name = test["name"]
                else:
                    test_name = test
                logging.info("test_name: "+test_name)
                # If we have no workers available, immediately wait
                if (len(threads_in_flight) == self._parallel_jobs):
                    self._wait_for_something_to_finish(threads_in_flight)

                if (self._work_remains(test)):
                    work_to_do = True
                    if (test_name not in threads_in_flight):
                        test_phase, test_status = self._get_test_data(test)
                        expect(test_status != TEST_PENDING_STATUS, test_name)
                        next_phase = self._phases[self._phases.index(test_phase) + 1]
                        procs_needed = self._get_procs_needed(test_name, next_phase)

                        if (procs_needed <= self._proc_pool):
                            self._proc_pool -= procs_needed

                            # Necessary to print this way when multiple threads printing
                            sys.stdout.write("Starting %s for test %s with %d procs\n" % (next_phase, test_name, procs_needed))

                            self._update_test_status(test, next_phase, TEST_PENDING_STATUS)
                            t = threading.Thread(target=self._consumer,
                                                 args=(test, next_phase, getattr(self, "_%s_phase" % next_phase.lower()) ))
                            threads_in_flight[test_name] = (t, procs_needed)
                            t.start()
                            num_threads_launched_this_iteration += 1

            if (not work_to_do):
                break

            if (num_threads_launched_this_iteration == 0):
                # No free resources, wait for something in flight to finish
                self._wait_for_something_to_finish(threads_in_flight)

        for thread_info in threads_in_flight.values():
            thread_info[0].join()

    ###########################################################################
    def _setup_cs_files(self):
    ###########################################################################
        try:
            python_libs_root = CIME.utils.get_python_libs_root()
            acme_scripts_root = CIME.utils.get_acme_scripts_root()
            template_file = os.path.join(python_libs_root, "cs.status.template")
            template = open(template_file, "r").read()
            template = template.replace("<PATH>", acme_scripts_root).replace("<TESTID>", self._test_id)

            cs_status_file = os.path.join(self._test_root, "cs.status.%s" % self._test_id)
            with open(cs_status_file, "w") as fd:
                fd.write(template)
            os.chmod(cs_status_file, os.stat(cs_status_file).st_mode | stat.S_IXUSR | stat.S_IXGRP)

            template_file = os.path.join(python_libs_root, "cs.submit.template")
            template = open(template_file, "r").read()
            build_cmd = "./*.test_build" if self._no_build else ":"
            run_cmd = "./*.test" if self._no_batch else "./*.submit"
            template = template.replace("<BUILD_CMD>", build_cmd).replace("<RUN_CMD>", run_cmd).replace("<TESTID>", self._test_id)

            if (self._no_build or self._no_run):
                cs_submit_file = os.path.join(self._test_root, "cs.submit.%s" % self._test_id)
                with open(cs_submit_file, "w") as fd:
                    fd.write(template)
                os.chmod(cs_submit_file, os.stat(cs_submit_file).st_mode | stat.S_IXUSR | stat.S_IXGRP)

        except Exception as e:
            logging.warning("FAILED to set up cs files: %s" % str(e))

    ###########################################################################
    def create_test(self):
    ###########################################################################
        """
        Main API for this class.

        Return True if all tests passed.
        """
        start_time = time.time()

        # Tell user what will be run
        print "RUNNING TESTS:"
        for test in self._tests:
            if (type(test) == type(dict())):
                test_name = test["name"]
            else:
                test_name = test
            print " ", test_name
            if(self._cime_model == "cesm"):
                self._testspec.add_test(self._compiler, 'trythis', test_name)
        # TODO - documentation

        self._producer()

        expect(threading.active_count() == 1, "Leftover threads?")

        # Setup cs files
        self._setup_cs_files()

        # Return True if all tests passed
        print "At create_test close, state is:"
        rv = True
        for idx, test in enumerate(self._tests):
            phase, status = self._get_test_data(test)
            logging.debug("phase %s status %s" %(phase, status))
            if (status == TEST_PASS_STATUS and phase == RUN_PHASE):
                # Be cautious about telling the user that the test passed. This
                # status should match what they would see on the dashboard. Our
                # self._test_states does not include comparison fail information,
                # so we need to parse test status.
                test_status_file = os.path.join(self._get_test_dir(test["name"]), TEST_STATUS_FILENAME)
                status = wait_for_tests.interpret_status_file(test_status_file)[1]

            if (status not in [TEST_PASS_STATUS, TEST_PENDING_STATUS]):
                print "%s %s (phase %s)" % (status, test["name"], phase)
                rv = False

            elif (test["name"] in self._tests_with_nl_problems):
                print "%s %s (but otherwise OK)" % (NAMELIST_FAIL_STATUS, test["name"])
                rv = False

            else:
                print status, test["name"], phase

            print "    Case dir: %s" % self._get_test_dir(test["name"])

        print "create_test took", time.time() - start_time, "seconds"

        if(self._cime_model == "cesm"):
            self._testspec.write()

        return rv

    def  _get_tests_from_xml(self,xml_machine=None,xml_category=None,xml_compiler=None, xml_testlist=None,
                             machine=None, compiler=None):
        """
        Parse testlists for a list of tests
        """
        listoftests = []
        testlistfiles = []
        if(machine is not None):
            thismach=machine
        if(compiler is not None):
            thiscompiler = compiler

        if(xml_testlist is not None):
             expect(os.path.isfile(xml_testlist), "Testlist not found or not readable "+xml_testlist)
             testlistfiles.append(xml_testlist)
        else:
            files = Files()
            test_spec_files = files.get_values("TESTS_SPEC_FILE","component")
            for spec_file in test_spec_files.viewvalues():
                if(os.path.isfile(spec_file)):
                    testlistfiles.append(spec_file)

        for testlistfile in testlistfiles:
            thistestlistfile = Testlist(testlistfile)
            newtests =  thistestlistfile.get_tests(xml_machine, xml_category, xml_compiler)
            for test in newtests:
                if(machine is None):
                    thismach = test["machine"]
                if(compiler is None):
                    thiscompiler = test["compiler"]
                test["name"] = "%s.%s.%s.%s_%s"%(test["testname"],test["grid"],test["compset"],thismach,thiscompiler)
                if ("testmods" in test):
                    (moddir, modname) = test["testmods"].split("/")
                    test["name"] += ".%s_%s"%(moddir, modname)
                logging.info("Adding test "+test["name"])
            listoftests += newtests

        return listoftests

    def _convert_testlist_to_dict(self,test_names):
        from copy import deepcopy
        listoftests = []
        test = {}
        for name in test_names:
            test["name"] = name
            listoftests.append(deepcopy(test))
        return listoftests
