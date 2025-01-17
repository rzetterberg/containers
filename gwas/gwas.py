# README:
# gwas.py converts pheno+dict files into format compatible with plink or regenie analysis. Other association analyses can be added later.
# gwas.py automatically decides whether to run logistic or linear regression by looking at the data dictionary for requested phenotypes 
# gwas.py generates all scripts needed to run the analysis, and to convert the results back to a standard summary statistics format

import argparse
import logging, time, sys, traceback, socket, getpass, six, os
import pandas as pd
import numpy as np
from scipy import stats

__version__ = '1.0.0'
MASTHEAD = "***********************************************************************\n"
MASTHEAD += "* gwas.py: pipeline for GWAS analysis\n"
MASTHEAD += "* Version {V}\n".format(V=__version__)
MASTHEAD += "* (C) 2021 Oleksandr Frei and Bayram Akdeniz\n"
MASTHEAD += "* Norwegian Centre for Mental Disorders Research / University of Oslo\n"
MASTHEAD += "* GNU General Public License v3\n"
MASTHEAD += "***********************************************************************\n"

# https://stackoverflow.com/questions/27433316/how-to-get-argparse-to-read-arguments-from-a-file-with-an-option-rather-than-pre
class LoadFromFile (argparse.Action):
    def __call__ (self, parser, namespace, values, option_string=None):
        with values as f:
            contents = '\n'.join([x for x in f.readlines() if (not x.strip().startswith('#'))])

        data = parser.parse_args(contents.split(), namespace=namespace)
        for k, v in vars(data).items():
            if v and k != option_string.lstrip('-'):
                setattr(namespace, k, v)

def parse_args(args):
    parser = argparse.ArgumentParser(description="A pipeline for GWAS analysis")

    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--argsfile', type=open, action=LoadFromFile, default=None, help="file with additional command-line arguments, e.g. those configuration settings that are the same across all of your runs")
    parent_parser.add_argument("--out", type=str, default="gwas", help="prefix for the output files (<out>.covar, <out>.pheno, etc)")
    parent_parser.add_argument("--log", type=str, default=None, help="file to output log, defaults to <out>.log")
    parent_parser.add_argument("--log-sensitive", action="store_true", default=False, help="allow sensitive (individual-level) information in <out>.log. Use with caution. This may help debugging errors, but then you must pay close attention to avoid exporting the .log files out of your security environent. It's recommended to delete .log files after  you've investigate the problem.")

    slurm_parser = argparse.ArgumentParser(add_help=False)
    slurm_parser.add_argument("--slurm-job-name", type=str, default="gwas", help="SLURM --job-name argument")
    slurm_parser.add_argument("--slurm-account", type=str, default="p697_norment", help="SLURM --account argument")
    slurm_parser.add_argument("--slurm-time", type=str, default="06:00:00", help="SLURM --time argument")
    slurm_parser.add_argument("--slurm-cpus-per-task", type=int, default=16, help="SLURM --cpus-per-task argument")
    slurm_parser.add_argument("--slurm-mem-per-cpu", type=str, default="8000M", help="SLURM --mem-per-cpu argument")
    slurm_parser.add_argument("--module-load", type=str, nargs='+', default=['singularity/3.7.1'], help="list of modules to load")
    slurm_parser.add_argument("--comorment-folder", type=str, default='/cluster/projects/p697/github/comorment', help="folder containing 'containers' subfolder with a full copy of https://github.com/comorment/containers")
    slurm_parser.add_argument("--singularity-bind", type=str, default='$COMORMENT/containers/reference:/REF:ro', help="translates to SINGULARITY_BIND variable in SLURM scripts")
    
    parent_parser.add_argument("--chr2use", type=str, default='1-22', help="Chromosome ids to use "
         "(e.g. 1,2,3 or 1-4,12,16-20). Used when '@' is present in --bed-test (or similar arguments), but also to specify for which chromosomes to run the association testing.")

    subparsers = parser.add_subparsers(dest='cmd')
    subparsers.required = True

    parser_gwas_add_arguments(args=args, func=execute_gwas, parser=subparsers.add_parser("gwas", parents=[parent_parser, slurm_parser], help='perform gwas analysis'))
    parser_merge_plink2_add_arguments(args=args, func=merge_plink2, parser=subparsers.add_parser("merge-plink2", parents=[parent_parser], help='merge plink2 sumstats files'))
    parser_merge_regenie_add_arguments(args=args, func=merge_regenie, parser=subparsers.add_parser("merge-regenie", parents=[parent_parser], help='merge regenie sumstats files'))

    return parser.parse_args(args)

def parser_gwas_add_arguments(args, func, parser):
    parser.add_argument("--pheno-file", type=str, default=None, help="phenotype file, according to CoMorMent spec")
    parser.add_argument("--dict-file", type=str, default=None, help="phenotype dictionary file, defaults to <pheno>.dict")

    # genetic files to use. All must share the same set of individuals. Currently this assumption is not validated.
    parser.add_argument("--fam", type=str, default=None, help="optional argument pointing to a plink .fam with lift of individuals with genetic data; is not provided, the list will be taken from --bed-fit or --bed-test file.")
    parser.add_argument("--bed-fit", type=str, default=None, help="plink bed/bim/fam file to use in a first step of mixed effect models")
    parser.add_argument("--bed-test", type=str, default=None, help="plink bed/bim/fam file to use in association testing; supports '@' as a place holder for chromosome labels")
    parser.add_argument("--bgen-fit", type=str, default=None, help=".bgen file to use in a first step of mixed effect models")
    parser.add_argument("--bgen-test", type=str, default=None, help=".bgen file to use in association testing; supports '@' as a place holder for chromosome labels")

    parser.add_argument("--covar", type=str, default=[], nargs='+', help="covariates to control for (must be columns of the --pheno-file); individuals with missing values for any covariates will be excluded not just from <out>.covar, but also from <out>.pheno file")
    parser.add_argument("--variance-standardize", type=str, default=None, nargs='*', help="the list of continuous phenotypes to standardize variance; accept the list of columns from the --pheno file (if empty, applied to all); doesn't apply to dummy variables derived from NOMINAL or BINARY covariates.")
    parser.add_argument("--pheno", type=str, default=[], nargs='+', help="target phenotypes to run GWAS (must be columns of the --pheno-file")
    parser.add_argument("--pheno-na-rep", type=str, default='NA', help="missing data representation for phenotype file (regenie: NA, plink: -9)")
    parser.add_argument('--analysis', type=str, default=['plink2', 'regenie'], nargs='+', choices=['plink2', 'regenie'])

    parser.set_defaults(func=func)

def parser_merge_plink2_add_arguments(args, func, parser):
    parser.add_argument("--sumstats", type=str, default=None, help="sumstat file produced by plink2, containing @ as chromosome label place holder")
    parser.set_defaults(func=func)

def parser_merge_regenie_add_arguments(args, func, parser):
    parser.add_argument("--sumstats", type=str, default=None, help="sumstat file produced by plink2, containing @ as chromosome label place holder")
    parser.set_defaults(func=func)

def extract_variables(df, variables, pheno_dict_map, log):
    cat_vars = [x for x in variables if pheno_dict_map[x] == 'NOMINAL']
    other_vars =  ['FID', 'IID'] + [x for x in variables if pheno_dict_map[x] != 'NOMINAL']

    dummies=df[other_vars]
    for var in cat_vars:
        new = pd.get_dummies(df[var], prefix=var)
        dummies = dummies.join(new)

        #drop most frequent variable for ref category
        drop_col = df.groupby([var]).size().idxmax()
        dummies.drop('{}_{}'.format(var, drop_col), axis=1, inplace=True)

        log.log('Variable {} will be extracted as dummie, dropping {} label (most frequent)'.format(var, drop_col))
    return dummies.copy()

def fix_and_validate_chr2use(args, log):
    arg_dict = vars(args)
    chr2use_arg = arg_dict["chr2use"]
    chr2use = []
    for a in chr2use_arg.split(","):
        if "-" in a:
            start, end = [int(x) for x in a.split("-")]
            chr2use += [str(x) for x in range(start, end+1)]
        else:
            chr2use.append(a.strip())
    arg_dict["chr2use"] = chr2use

def fix_and_validate_args(args, log):
    if not args.pheno_file: raise ValueError('--pheno-file is required.')
    if not args.pheno: raise ValueError('--pheno is required.')    

    if ('plink2' in args.analysis) and ('regenie' in args.analysis):
        raise ValueError('--analysis can not have both --plink2 and --regenie, please choose one of these.')

    # validate that some of genetic data is provided as input
    if (not args.bed_test) and (not args.bgen_test):
        raise ValueError('--bed-test or --bgen-test must be specified')
    if ('regenie' in 'analysis') and (not args.bed_fit) and (not args.bgen_fit):
        raise ValueError('--bed-fit or --bgen-fit must be specified for --analysis regenie')

    if args.fam is None:
        if (args.bed_fit is None) and (args.bed_test is None):
            raise ValueError('please specify --fam argument in plink format, containing the same set of individuals as your --bgen files')
        args.fam = (args.bed_fit if args.bed_fit else args.bed_test) + '.fam'
        if '@' in args.fam: args.fam = args.fam.replace('@', args.chr2use[0])
    check_input_file(args.fam)

    check_input_file(args.pheno_file)
    if not args.dict_file: args.dict_file = args.pheno_file + '.dict'
    check_input_file(args.dict_file)

def make_regenie_commands(args, logistic, step):
    geno_fit = args.bed_fit if (args.bed_fit is not None) else args.bgen_fit
    geno_test = args.bed_test if (args.bed_test is not None) else args.bgen_test
    geno_test = geno_test.replace('@', '${SLURM_ARRAY_TASK_ID}')
    sample = (remove_suffix(geno_test, ".bgen") + '.sample')

    if ('@' in geno_fit): raise(ValueError('--bed-fit or --bgen-fit contains "@", hense it is incompatible with regenie step1 which require a single file'))

    cmd = "$REGENIE " + \
        (" --bt" if logistic else "") + \
        " --phenoFile {}.pheno".format(args.out) + \
        (" --covarFile {}.covar".format(args.out) if args.covar else "")

    cmd_step1 = ' --step 1 --bsize 1000' + \
        " --out {}.regenie.step1".format(args.out) + \
        (" --bed {} --ref-first".format(geno_fit) if (args.bed_fit is not None) else "") + \
        (" --bgen {} --ref-first".format(geno_fit) if (args.bgen_fit is not None) else "") + \
        (" --bt" if logistic else "") + \
        " --lowmem --lowmem-prefix {}.regenie_tmp_preds".format(args.out)

    cmd_step2 = ' --step 2 --bsize 400' + \
        " --out {}_chr${{SLURM_ARRAY_TASK_ID}}".format(args.out) + \
        (" --bed {} --ref-first".format(geno_test) if (args.bed_test is not None) else "") + \
        (" --bgen {} --ref-first --sample {}".format(geno_test, sample) if (args.bgen_test is not None) else "") + \
        (" --bt --firth 0.01 --approx" if logistic else "") + \
        " --pred {}.regenie.step1_pred.list".format(args.out) + \
        " --chr ${SLURM_ARRAY_TASK_ID}"

    return (cmd + cmd_step1) if step==1 else (cmd + cmd_step2)

def make_regenie_merge(args, logistic):
    cmd = ''
    for pheno in args.pheno:
        cmd += '$PYTHON gwas.py merge-regenie ' + \
            ' --sumstats {out}_chr@_{pheno}.regenie'.format(out=args.out, pheno=pheno) + \
            ' --out {out}_{pheno}.regenie '.format(out=args.out, pheno=pheno) + \
            ' --chr2use {} '.format(','.join(args.chr2use)) + \
            '\n'
    return cmd

def make_plink2_merge(args, logistic):
    cmd = ''
    for pheno in args.pheno:
        cmd += '$PYTHON gwas.py merge-plink2 ' + \
            ' --sumstats {out}_chr@.{pheno}.glm.{what}'.format(out=args.out, pheno=pheno, what=('logistic' if logistic else 'linear')) + \
            ' --out {out}_{pheno}.plink2 '.format(out=args.out, pheno=pheno) + \
            ' --chr2use {} '.format(','.join(args.chr2use)) + \
            '\n'
    return cmd

def remove_suffix(text, suffix):
    return text[:-len(suffix)] if text.endswith(suffix) and len(suffix) != 0 else text

def make_plink2_commands(args, logistic):
    geno = args.bed_test if (args.bed_test is not None) else args.bgen_test
    geno = geno.replace('@', '${SLURM_ARRAY_TASK_ID}')
    sample = remove_suffix(geno, ".bgen") + '.sample'

    cmd = "$PLINK2 " + \
        (" --bfile " + geno                                      if (args.bed_test is not None) else "") + \
        (" --bgen {} ref-first --sample {}".format(geno, sample) if (args.bgen_test is not None) else "") + \
        " --no-pheno " + \
        " --chr ${SLURM_ARRAY_TASK_ID}" + \
        " --glm hide-covar" + \
        " --pheno {}.pheno".format(args.out) + \
        (" --covar {}.covar".format(args.out) if args.covar else "") + \
        " --out {}_chr${{SLURM_ARRAY_TASK_ID}}".format(args.out)
    return cmd


def make_slurm_header(args, array=False):
    return """#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --account={account}
#SBATCH --time={time}
#SBATCH --cpus-per-task={cpus_per_task}
#SBATCH --mem-per-cpu={mem_per_cpu}
{array}

{modules}

export COMORMENT={comorment_folder}
export SINGULARITY_BIND="{singularity_bind}"
export SIF=$COMORMENT/containers/singularity

export PLINK2="singularity exec --home $PWD:/home $SIF/gwas.sif plink2"
export REGENIE="singularity exec --home $PWD:/home $SIF/gwas.sif regenie"
export PYTHON="singularity exec --home $PWD:/home $SIF/python3.sif python"

""".format(array="#SBATCH --array={}".format(','.join(args.chr2use)) if array else "",
           modules = '\n'.join(['module load {}'.format(x) for x in args.module_load]),
           job_name = args.slurm_job_name,
           account = args.slurm_account,
           time = args.slurm_time,
           cpus_per_task = args.slurm_cpus_per_task,
           mem_per_cpu = args.slurm_mem_per_cpu,
           comorment_folder = args.comorment_folder,
           singularity_bind = args.singularity_bind)

def execute_gwas(args, log):
    fix_and_validate_chr2use(args, log)
    fix_and_validate_args(args, log)

    fam = read_fam(args, args.fam)
    pheno, pheno_dict = read_comorment_pheno(args, args.pheno_file, args.dict_file)
    pheno_dict_map = dict(zip(pheno_dict['FIELD'], pheno_dict['TYPE']))

    missing_cols = [str(c) for c in args.pheno if (c not in pheno.columns)]
    if missing_cols: raise(ValueError('--pheno not present in --pheno-file: {}'.format(', '.join(missing_cols))))

    pheno_types = [pheno_dict_map[pheno] for pheno in args.pheno]
    for pheno_type in pheno_types:
        if pheno_type not in ['BINARY', 'CONTINUOUS']:
            raise(ValueError('only BINARY or CONTINOUS varibales can be used as --pheno'))
    pheno_type = list(set(pheno_types))
    if len(pheno_type) != 1:
        raise(ValueError('--pheno variables has a mix of BINARY and CONTINUOS types'))
    pheno_type = pheno_type[0]
    logistic = (pheno_type=='BINARY')

    if 'FID' in pheno.columns:
        log.log("FID column is present in --pheno-file; this values will be ignored and replaced with FID column from --fam file")
        del pheno['FID']

    log.log("merging --pheno and --fam file...")
    n = len(pheno); pheno = pd.merge(pheno, fam[['IID', 'FID']], on='IID', how='inner')
    pheno_dict_map['FID'] = 'FID'
    log.log("n={} individuals remain after merging, n={} removed".format(len(pheno), n-len(pheno)))

    if args.covar:
        missing_cols = [str(c) for c in args.covar if (c not in pheno.columns)]
        if missing_cols: raise(ValueError('--covar not present in --pheno-file: {}'.format(', '.join(missing_cols))))

        log.log("filtering individuals with missing covariates...")
        for var in args.covar:
            mask = pheno[var].isnull()
            if np.any(mask):
                n = len(pheno)
                pheno = pheno[~mask].copy()
                log.log("n={} individuals remain after removing n={} individuals with missing value in {} covariate".format(len(pheno), n-len(pheno), var))

    if len(pheno) <= 1:
        raise ValueError('Too few individuals remain for analysis, exit.')

    if args.variance_standardize is not None:
        if len(args.variance_standardize) == 0:
            args.variance_standardize = [col for col in pheno.columns if (pheno_dict_map[col] == 'CONTINUOUS')]

        for col in args.variance_standardize:
            if (col not in pheno.columns) or (pheno_dict_map[col] != 'CONTINUOUS'):
                raise ValueError('Can not apply --variance-standardize to {}, column is missing or its type is other than CONTINUOUS'.fromat(col))

            mean = np.nanmean(pheno[col].values)
            std = np.nanstd(pheno[col].values, ddof=1)
            log.log('phenotype {} has mean {:.5f} and std {:.5f}. Normalizing to 0.0 mean and 1.0 std'.format(col, mean, std))
            pheno[col] = (pheno[col].values - mean) / std

    log.log("extracting covariates...")
    if args.covar:
        covar_output = extract_variables(pheno, args.covar, pheno_dict_map, log)
        log.log('writing {} columns (including FID, IID) for n={} individuals to {}.covar'.format(covar_output.shape[1], len(covar_output), args.out))
        covar_output.to_csv(args.out + '.covar', index=False, sep='\t')
    else:
        log.log('--covar not specified')

    log.log("extracting phenotypes...")
    pheno_output = extract_variables(pheno, args.pheno, pheno_dict_map, log)
    for var in args.pheno:
        if logistic:
            log.log('variable: {}, cases: {}, controls: {}, missing: {}'.format(var, np.sum(pheno[var]=='1'), np.sum(pheno[var]=='0'), np.sum(pheno[var].isnull())))
        else:
            log.log('variable: {}, missing: {}'.format(var, np.sum(pheno[var].isnull())))

    if ('plink2' in args.analysis) and logistic:
        log.log('mapping case/control variables from 1/0 to 2/1 coding')
        for var in args.pheno:
            pheno_output[var] = pheno_output[var].map({'0':'1', '1':'2'}).values

    log.log('writing {} columns (including FID, IID) for n={} individuals to {}.pheno'.format(pheno_output.shape[1], len(pheno_output), args.out))
    pheno_output.to_csv(args.out + '.pheno', na_rep='NA', index=False, sep='\t')

    log.log('all --pheno variables have type: {}, selected analysis: {}'.format(pheno_type, 'logistic' if logistic else 'linear'))

    cmd_file = args.out + '_cmd.sh'
    if os.path.exists(cmd_file): os.remove(cmd_file)
    submit_jobs = []
    if 'plink2' in args.analysis:
        with open(args.out + '_plink2.1.job', 'w') as f:
            f.write(make_slurm_header(args, array=True) + make_plink2_commands(args, logistic) + '\n') 
        with open(args.out + '_plink2.2.job', 'w') as f:
            f.write(make_slurm_header(args, array=False) + make_plink2_merge(args, logistic) + '\n') 
        with open(cmd_file, 'a') as f:
            f.write('for SLURM_ARRAY_TASK_ID in {}; do {}; done\n'.format(' '.join(args.chr2use), make_plink2_commands(args, logistic)))
            f.write(make_plink2_merge(args, logistic) + '\n')
        append_job(args.out + '_plink2.1.job', False, submit_jobs)
        append_job(args.out + '_plink2.2.job', True, submit_jobs)
    if 'regenie' in args.analysis:
        with open(args.out + '_regenie.1.job', 'w') as f:
            f.write(make_slurm_header(args, array=False) + make_regenie_commands(args, logistic, step=1) + '\n') 
        with open(args.out + '_regenie.2.job', 'w') as f:
            f.write(make_slurm_header(args, array=True) + make_regenie_commands(args, logistic, step=2) + '\n') 
        with open(args.out + '_regenie.3.job', 'w') as f:
            f.write(make_slurm_header(args, array=False) + make_regenie_merge(args, logistic) + '\n') 
        with open(cmd_file, 'a') as f:
            f.write(make_regenie_commands(args, logistic, step=1) + '\n')
            f.write('for SLURM_ARRAY_TASK_ID in {}; do {}; done\n'.format(' '.join(args.chr2use), make_regenie_commands(args, logistic, step=2)))
            f.write(make_regenie_merge(args, logistic) + '\n') 
        append_job(args.out + '_regenie.1.job', False, submit_jobs)
        append_job(args.out + '_regenie.2.job', True, submit_jobs)
        append_job(args.out + '_regenie.3.job', True, submit_jobs)
    log.log('To submit all jobs via SLURM, use the following scripts, otherwise execute commands from {}.'.format(cmd_file))
    print('\n'.join(submit_jobs))

def merge_plink2(args, log):
    fix_and_validate_chr2use(args, log)
    pattern = args.sumstats
    stat = 'T_STAT' if  pattern.endswith('.glm.linear') else 'Z_STAT'
    effect_cols = (['BETA', "SE"] if  pattern.endswith('.glm.linear') else ['OR', 'LOG(OR)_SE'])
    df=pd.concat([pd.read_csv(pattern.replace('@', chri), delim_whitespace=True)[['ID', '#CHROM', 'POS', 'REF', 'ALT', 'A1', 'OBS_CT', stat, 'P']+effect_cols] for chri in args.chr2use])
    df['A2'] = df['REF']; idx=df['A2']==df['A1']; df.loc[idx, 'A2']=df.loc[idx, 'ALT']; del df['REF']; del df['ALT']
    if not pattern.endswith('.glm.linear'):
        df['BETA'] = np.log(df['OR'])
        df.rename(columns={'LOG(OR)_SE':'SE'}, inplace=True)
    df.dropna().rename(columns={'ID':'SNP', '#CHROM':'CHR', 'POS':'BP', 'OBS_CT':'N', stat:'Z', 'P':'PVAL'})[['SNP', 'CHR', 'BP', 'A1', 'A2', 'N', 'Z', 'BETA', 'SE', 'PVAL']].to_csv(args.out, index=False, sep='\t')
    os.system('gzip -f ' + args.out)

def merge_regenie(args, log):
    fix_and_validate_chr2use(args, log)
    pattern = args.sumstats
    df=pd.concat([pd.read_csv(pattern.replace('@', chri), delim_whitespace=True)[['ID', 'CHROM', 'BETA', 'SE', 'GENPOS', 'ALLELE0', 'ALLELE1', 'N', 'LOG10P']] for chri in args.chr2use])
    df['PVAL']=np.power(10, -df['LOG10P'])
    df['Z'] = -stats.norm.ppf(df['PVAL'].values*0.5)*np.sign(df['BETA']).astype(np.float64)
    df.dropna().rename(columns={'ID':'SNP', 'CHROM':'CHR', 'GENPOS':'BP', 'ALLELE0':'A2', 'ALLELE1':'A1'})[['SNP', 'CHR', 'BP', 'A1', 'A2', 'N', 'Z', 'BETA', 'SE', 'PVAL']].to_csv(args.out,index=False, sep='\t')
    os.system('gzip -f ' + args.out)

def check_input_file(fname, chr2use=None):
    if (chr2use is not None) and ('@' in fname):
        for chri in chr2use:
            if not os.path.isfile(fname.replace('@', str(chri))): 
                raise ValueError("Input file does not exist: {f}".format(f=fname.replace('@', str(chri))))
    else:
        if not os.path.isfile(fname): 
            raise ValueError("Input file does not exist: {f}".format(f=fname))

def sec_to_str(t):
    '''Convert seconds to days:hours:minutes:seconds'''
    [d, h, m, s, n] = six.moves.reduce(lambda ll, b : divmod(ll[0], b) + ll[1:], [(t, 1), 60, 60, 24])
    f = ''
    if d > 0:
        f += '{D}d:'.format(D=d)
    if h > 0:
        f += '{H}h:'.format(H=h)
    if m > 0:
        f += '{M}m:'.format(M=m)

    f += '{S}s'.format(S=s)
    return f

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

class Logger(object):
    '''
    Lightweight logging.
    '''
    def __init__(self, fh, mode):
        self.fh = fh
        self.log_fh = open(fh, mode) if (fh is not None) else None

        # remove error file from previous run if it exists
        try:
            if fh is not None: os.remove(fh + '.error')
        except OSError:
            pass

    def log(self, msg):
        '''
        Print to log file and stdout with a single command.
        '''
        eprint(msg)
        if self.log_fh:
            self.log_fh.write(str(msg).rstrip() + '\n')
            self.log_fh.flush()

    def error(self, msg):
        '''
        Print to log file, error file and stdout with a single command.
        '''
        eprint(msg)
        if self.log_fh:
            self.log_fh.write(str(msg).rstrip() + '\n')
            with open(self.fh + '.error', 'w') as error_fh:
                error_fh.write(str(msg).rstrip() + '\n')

def read_fam(args, fam_file):
    log.log('reading {}...'.format(fam_file))
    fam = pd.read_csv(fam_file, delim_whitespace=True, header=None, names='FID IID FatherID MotherID SEX PHENO'.split(), dtype=str)
    log.log('done, {} rows, {} cols'.format(len(fam), fam.shape[1]))
    if args.log_sensitive: log.log(fam.head())
    if np.any(fam['IID'].duplicated()): raise(ValueError("IID column has duplicated values in --fam file"))
    return fam

def append_job(job, depends_on_previous, job_list):
    if depends_on_previous:
        job_list.append('RES=$(sbatch --dependency=afterany:${{RES##* }} {})'.format(job))
    else:
        job_list.append('RES=$(sbatch {})'.format(job))

def read_comorment_pheno(args, pheno_file, dict_file):
    log.log('reading {}...'.format(pheno_file))
    pheno = pd.read_csv(pheno_file, sep=',', dtype=str)
    log.log('done, {} rows, {} cols'.format(len(pheno), pheno.shape[1]))
    if args.log_sensitive: log.log(pheno.head())
    
    log.log('reading {}...'.format(pheno_file))
    pheno_dict = pd.read_csv(dict_file, sep=',')
    log.log('done, {} rows, {} cols, header:'.format(len(pheno_dict), pheno_dict.shape[1]))
    log.log(pheno_dict.head())

    # validation logic for dict file
    if 'COLUMN' in pheno_dict.columns: pheno_dict.rename(columns={'COLUMN':'FIELD'}, inplace=True)
    if 'FIELD' not in pheno_dict.columns: raise(ValueError('--dict must include FIELD column'))
    if 'TYPE' not in pheno_dict.columns: raise(ValueError('--dict must include TYPE column'))
    pheno_dict['TYPE'] = pheno_dict['TYPE'].str.upper()
    extra_values = list(set(pheno_dict['TYPE'].values).difference({'IID', 'CONTINUOUS', 'BINARY', 'NOMINAL'}))
    if len(extra_values) > 0: raise(ValueError('TYPE column in --dict can only have IID, CONTINUOUS, BINARY and NOMINAL - found other values, e.g. {}'.format(extra_values[0])))

    # validation logic for pheno file
    if 'IID' not in pheno: raise(ValueError('IID column not present in --pheno-file'))
    if np.any(pheno['IID'].duplicated()): raise(ValueError('IID column has duplicated values in --pheno-file'))
    missing_cols = [str(c) for c in pheno.columns if (c not in pheno_dict['FIELD'].values)]
    if missing_cols: raise(ValueError('--pheno-file columns not present in --dict: {}'.format(', '.join(missing_cols))))

    pheno_dict_map = dict(zip(pheno_dict['FIELD'], pheno_dict['TYPE']))
    for c in pheno.columns:
        if pheno_dict_map[c]=='BINARY':
            bad_format = pheno[~pheno[c].isnull() & ~pheno[c].isin(['1', '0'])]
            if len(bad_format)>0:
                if args.log_sensitive: log.log(bad_format.head())
                raise(ValueError('BINARY column {} has values other than 0 or 1; see above for offending rows (if not shown, re-run with --log-sensitive argument)'.format(c)))
        if pheno_dict_map[c]=='CONTINUOUS':
            pheno[c] = pheno[c].astype(float)

    return pheno, pheno_dict

if __name__ == "__main__":
    args = parse_args(sys.argv[1:])

    if args.out is None:
        raise ValueError('--out is required.')

    log = Logger(args.log if args.log else (args.out + '.log'), 'w')
    start_time = time.time()

    try:
        defaults = vars(parse_args([sys.argv[1]]))
        opts = vars(args)
        non_defaults = [x for x in opts.keys() if opts[x] != defaults[x]]
        header = MASTHEAD
        header += "Call: \n"
        header += './gwas.py {} \\\n'.format(sys.argv[1])
        options = ['\t--'+x.replace('_','-')+' '+str(opts[x]).replace('\t', '\\t')+' \\' for x in non_defaults]
        header += '\n'.join(options).replace('True','').replace('False','')
        header = header[0:-1]+'\n'
        log.log(header)
        log.log('Beginning analysis at {T} by {U}, host {H}'.format(T=time.ctime(), U=getpass.getuser(), H=socket.gethostname()))

        # run the analysis
        args.func(args, log)

    except Exception:
        log.error( traceback.format_exc() )
        raise

    finally:
        log.log('Analysis finished at {T}'.format(T=time.ctime()) )
        time_elapsed = round(time.time()-start_time,2)
        log.log('Total time elapsed: {T}'.format(T=sec_to_str(time_elapsed)))
