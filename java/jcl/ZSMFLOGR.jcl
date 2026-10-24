//ZSMFLOGR JOB (ACCT),'SMF LOGSTREAM OTEL',CLASS=A,MSGCLASS=X,
//             NOTIFY=&SYSUID,REGION=0M,LINES=500000
//*--------------------------------------------------------------------
//* Nachladen/Aufholen: SMF-Saetze eines Zeitraums aus dem Log Stream
//* ueber den IFASEXIT-Subsystem-Exit lesen und per OTLP senden.
//* FROM/TO in lokaler Zeit (LOCAL): z. B. Inhalt von zsmf.checkpoint
//* des Echtzeit-Laeufers. Doppelte Saetze verwirft Loki selbst.
//* Benutzer braucht READ auf den Log Stream (Klasse LOGSTRM).
//*--------------------------------------------------------------------
//JAVA     EXEC PGM=JVMLDM17,REGION=0M,
//             PARM='/ zsmf.Main'
//STEPLIB  DD DSN=JAVA.SJVMLOAD,DISP=SHR     <- Ladebibl. JZOS anpassen
//SMFIN    DD DSN=IFASMF.DEFAULT,
//            RECFM=VB,LRECL=32756,BLKSIZE=32760,
//            SUBSYS=(LOGR,IFASEXIT,
//            'FROM=(2026/297,08:00),TO=YOUNGEST,LOCAL',
//            'SID(SYSA)')
//SYSPRINT DD SYSOUT=*
//SYSOUT   DD SYSOUT=*
//STDOUT   DD SYSOUT=*
//STDERR   DD SYSOUT=*
//STDENV   DD *
export JAVA_HOME=/usr/lpp/java/J17.0_64
export APP=/u/zuser/zsmf
export CLASSPATH=$APP/zsmf-otel.jar
export LIBPATH=$JAVA_HOME/lib/s390x:$JAVA_HOME/lib/s390x/j9vm
export ZSMF_ARGS="--source logstream:DD:SMFIN --types 30,80,119
 --otlp http://collector.example.com:4318 --tz Europe/Berlin"
/*
