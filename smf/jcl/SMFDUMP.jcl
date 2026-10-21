//SMFDUMP  JOB (ACCT),'SMF FUER OTEL',CLASS=A,MSGCLASS=X,
//             NOTIFY=&SYSUID,REGION=0M,LINES=500000
//*--------------------------------------------------------------------
//* SMF-Saetze fuer smf_otel.py herausziehen: 30 (Subtyp 4,5), 80,
//* 119 (Subtyp 11,12). Ergebnis: &SYSUID..SMF.OTEL (RECFM=VBS).
//* Danach per FTP BINAER MIT RDW holen (siehe README):
//*   ftp> binary
//*   ftp> quote site rdw
//*   ftp> get 'USERID.SMF.OTEL' smf.bin
//*--------------------------------------------------------------------
//* Variante A: SMF in einem Log Stream (IFASMFDL). LSNAME anpassen.
//*--------------------------------------------------------------------
//DUMPLS   EXEC PGM=IFASMFDL
//SYSPRINT DD SYSOUT=*
//OUTDD1   DD DSN=&SYSUID..SMF.OTEL,DISP=(NEW,CATLG),
//            SPACE=(CYL,(50,50),RLSE),UNIT=SYSDA,
//            DCB=(RECFM=VBS,LRECL=32760,BLKSIZE=0)
//SYSIN    DD *
  LSNAME(IFASMF.DEFAULT,OPTIONS(DUMP))
  OUTDD(OUTDD1,TYPE(30(4,5),80,119(11,12)))
  RELATIVEDATE(BYDAY,0,1)
/*
//*--------------------------------------------------------------------
//* Variante B: SMF-Datensaetze SYS1.MANx (IFASMFDP). Statt Schritt
//* DUMPLS verwenden (DUMPLS dann loeschen), INDD-Name anpassen.
//*--------------------------------------------------------------------
//*DUMPDS  EXEC PGM=IFASMFDP
//*SYSPRINT DD SYSOUT=*
//*MAN1    DD DSN=SYS1.MAN1,DISP=SHR
//*OUTDD1  DD DSN=&SYSUID..SMF.OTEL,DISP=(NEW,CATLG),
//*           SPACE=(CYL,(50,50),RLSE),UNIT=SYSDA,
//*           DCB=(RECFM=VBS,LRECL=32760,BLKSIZE=0)
//*SYSIN   DD *
//*  INDD(MAN1,OPTIONS(DUMP))
//*  OUTDD(OUTDD1,TYPE(30(4,5),80,119(11,12)))
//*/*
